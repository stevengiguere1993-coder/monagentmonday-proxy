"""Suivi d'un immeuble en GESTION EXTERNE (retour Phil 2026-07-22).

La compagnie de gestion perçoit les loyers et refacture les travaux ;
Kratos garde un miroir de leur rapport :

    Paiements PAR LOGEMENT (pas de locataire connu) :
      GET    /immobilier/immeubles/{id}/paiements-externes?mois=YYYY-MM
      POST   /immobilier/paiements-externes {logement_id, mois, montant?}
      DELETE /immobilier/paiements-externes/{logement_id}?mois=YYYY-MM

    Tout le PORTEFEUILLE externe d'un mois (pour que la page Paiements
    générale les affiche à côté des immeubles internes — retour Phil
    2026-08-13) :
      GET    /immobilier/loyers/externes?mois=YYYY-MM&entreprise_id=N

    FACTURES PONCTUELLES (ex. 350 $ de plomberie pour l'app. 3 — jamais
    récurrentes, rattachées optionnellement à un logement) :
      GET    /immobilier/immeubles/{id}/factures-externes?annee=AAAA
      POST   /immobilier/immeubles/{id}/factures-externes
      PUT    /immobilier/factures-externes/{id}
      DELETE /immobilier/factures-externes/{id}
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DBSession
from app.core.permissions import visible_immeuble_ids
from app.models.immobilier import (
    Bail,
    BailStatus,
    FactureExterne,
    Immeuble,
    Locataire,
    Logement,
    PaiementExterne,
)
from app.services.loyer_echeance import seuil_retard

log = logging.getLogger(__name__)

router = APIRouter(prefix="/immobilier", tags=["immobilier-gestion-externe"])


def _require_volet(user: CurrentUser) -> None:
    volets = getattr(user, "volets", None)
    if volets is None or "immobilier" not in volets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Volet « Gestion immobilière » non autorisé.",
        )


def _parse_mois(mois: str) -> date:
    try:
        return datetime.strptime(mois + "-01", "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Format mois attendu : YYYY-MM."
        )


# ─── Paiements par logement ────────────────────────────────────────────


class PaiementExterneRow(BaseModel):
    logement_id: int
    logement_numero: str
    logement_status: str
    loyer_attendu: Optional[float] = None
    paye: bool = False
    #: MÊMES états que le suivi interne (``/loyers/overview``) pour que
    #: les deux surfaces se lisent pareil — retour Phil 2026-08-13 :
    #: "paye" | "partiel" | "retard" | "attente", plus "aucun" quand rien
    #: n'est attendu (logement vacant / hors location).
    etat: str = "aucun"
    #: CUMUL des montants reçus pour le mois (paiements partiels
    #: possibles — retour Phil 2026-07-22).
    montant: Optional[float] = None
    paye_le: Optional[date] = None
    #: Solde CUMULATIF dû sur l'unité (retour Phil 2026-09-09 : « en
    #: gestion externe, quand il y a un solde, il n'est pas suivi de
    #: mois en mois ») : Σ attendu − Σ reçu depuis le premier mois
    #: suivi de l'immeuble, borné à 0. Même lecture qu'en interne.
    solde_total: float = 0.0
    #: Un mois antérieur reste dû (badge, comme en interne).
    solde_anterieur: bool = False
    #: Nom du locataire (facultatif, saisi sur le logement ; bail
    #: résiduel en filet) — affiché « Gestion externe — Nom ».
    locataire_nom: Optional[str] = None


class PaiementExterneOverview(BaseModel):
    mois: str
    rows: List[PaiementExterneRow] = []
    total_attendu: float = 0.0
    total_recu: float = 0.0
    nb_payes: int = 0
    nb_impayes: int = 0


#: Tri commun : ce qui manque en haut, payés en bas (même ordre que la
#: page Paiements interne).
_ORDRE_ETAT = {"retard": 0, "partiel": 1, "attente": 2, "paye": 3, "aucun": 4}


def _mois_suivant(d: date) -> date:
    return date(d.year + (1 if d.month == 12 else 0), 1 if d.month == 12 else d.month + 1, 1)


async def _rows_externes(
    db, logements: list, month_start: date
) -> List[PaiementExterneRow]:
    """Croisement logements × paiements externes pour un mois, avec le
    SOLDE CUMULATIF de chaque unité et le nom du locataire.

    Partagé par la sous-page d'un immeuble et par la vue portefeuille
    (page Paiements générale) — une seule définition des états.
    """
    log_ids = [lg.id for lg in logements]
    if not log_ids:
        return []

    # Loyer attendu par logement — hiérarchie du loyer effectif (retour
    # client 2026-08-14) : en gestion EXTERNE, le loyer SAISI sur le
    # logement EST la vérité ; un bail résiduel ne sert que de filet
    # (loyer ET nom du locataire).
    from app.services.loyer_effectif import loyer_effectif
    from app.services.locatif_demarrage import get_demarrage

    bail_par_logement: dict[int, Bail] = {}
    for b in (
        await db.execute(
            select(Bail).where(
                Bail.logement_id.in_(log_ids),
                Bail.status == BailStatus.ACTIF.value,
            )
        )
    ).scalars().all():
        bail_par_logement[b.logement_id] = b
    noms_bail: dict[int, str] = {}
    loc_ids = {b.locataire_id for b in bail_par_logement.values() if b.locataire_id}
    if loc_ids:
        locs = {
            loc.id: loc.full_name
            for loc in (
                await db.execute(select(Locataire).where(Locataire.id.in_(list(loc_ids))))
            ).scalars().all()
        }
        for lid, b in bail_par_logement.items():
            if locs.get(b.locataire_id):
                noms_bail[lid] = locs[b.locataire_id]
    loyer_par_logement: dict[int, Optional[float]] = {}
    for lg in logements:
        b = bail_par_logement.get(lg.id)
        loyer_bail = float(b.loyer_mensuel or 0) if b is not None else None
        # Rien n'est attendu d'une unité ni louée (bail) ni « occupée ».
        if loyer_bail is None and lg.status != "occupe":
            loyer_par_logement[lg.id] = None
            continue
        loyer_par_logement[lg.id] = loyer_effectif(
            lg, loyer_bail, gestion_externe=True
        )

    # Historique complet jusqu'au mois affiché (cumul), borné au
    # démarrage du pôle.
    demarrage = await get_demarrage()
    hist: dict[int, dict[date, PaiementExterne]] = {}
    for p in (
        await db.execute(
            select(PaiementExterne).where(
                PaiementExterne.logement_id.in_(log_ids),
                PaiementExterne.mois_couvert <= month_start,
                PaiementExterne.mois_couvert >= demarrage,
            )
        )
    ).scalars().all():
        hist.setdefault(p.logement_id, {})[p.mois_couvert] = p
    # Borne d'entrée : le premier mois pour lequel un rapport du
    # gestionnaire a été saisi sur l'IMMEUBLE — avant, l'unité n'était pas
    # suivie, on ne lui fabrique pas de dette.
    imm_par_logement = {lg.id: lg.immeuble_id for lg in logements}
    premier_par_immeuble: dict[int, date] = {}
    for lid, mp in hist.items():
        iid = imm_par_logement.get(lid)
        m0 = min(mp)
        if iid is not None:
            premier_par_immeuble[iid] = min(premier_par_immeuble.get(iid, m0), m0)

    # Rien reçu passé l'échéance + délai de grâce = « retard », comme en
    # interne. Pas de jour d'échéance par bail ici : le gestionnaire
    # rapporte au mois, on garde le 1er + grâce.
    today = datetime.now(timezone.utc).date()
    en_retard = today > seuil_retard(month_start, 1)
    #: Mois strictement FUTUR : rien n'y est encore dû.
    mois_futur = month_start > today.replace(day=1)

    rows: List[PaiementExterneRow] = []
    for lg in logements:
        attendu_courant = loyer_par_logement.get(lg.id)
        mp = hist.get(lg.id, {})
        p = mp.get(month_start)
        attendu = attendu_courant
        # Mois DÉJÀ réglé : l'attendu FIGÉ à la saisie fait foi.
        if p is not None and p.loyer_attendu is not None:
            attendu = float(p.loyer_attendu)
        recu = (
            float(p.montant)
            if p is not None and p.montant is not None
            else ((attendu or 0.0) if p is not None else 0.0)
        )
        if p is not None and (attendu is None or recu >= attendu - 0.005):
            etat = "paye"
        elif p is not None:
            etat = "partiel"
        elif attendu:
            etat = "retard" if en_retard else "attente"
        else:
            etat = "aucun"

        # Solde cumulatif SIGNÉ des mois précédents (un trop-payé se
        # reporte), depuis la borne d'entrée de l'immeuble.
        cumul_ant = 0.0
        borne = premier_par_immeuble.get(lg.immeuble_id)
        if borne is not None and (attendu_courant is not None or mp):
            m = max(borne, demarrage)
            while m < month_start:
                pm = mp.get(m)
                if pm is not None:
                    att_m = (
                        float(pm.loyer_attendu)
                        if pm.loyer_attendu is not None
                        else (attendu_courant or 0.0)
                    )
                    recu_m = float(pm.montant) if pm.montant is not None else att_m
                    cumul_ant += att_m - recu_m
                elif attendu_courant is not None and lg.status == "occupe":
                    cumul_ant += attendu_courant
                m = _mois_suivant(m)
        solde_ant = max(0.0, cumul_ant)
        if mois_futur and p is None:
            solde_total = solde_ant
        else:
            solde_total = max(0.0, cumul_ant + (attendu or 0.0) - recu)
        rows.append(
            PaiementExterneRow(
                logement_id=lg.id,
                logement_numero=lg.numero,
                logement_status=lg.status,
                loyer_attendu=attendu,
                paye=etat == "paye",
                etat=etat,
                montant=round(recu, 2) if p is not None else None,
                paye_le=p.paye_le if p is not None else None,
                solde_total=round(solde_total, 2),
                solde_anterieur=solde_ant > 0.005,
                locataire_nom=(
                    getattr(lg, "locataire_externe_nom", None) or noms_bail.get(lg.id)
                ),
            )
        )
    rows.sort(
        key=lambda r: (_ORDRE_ETAT.get(r.etat, 9), r.logement_numero or "")
    )
    return rows


@router.get(
    "/immeubles/{immeuble_id}/paiements-externes",
    response_model=PaiementExterneOverview,
)
async def paiements_externes_overview(
    immeuble_id: int,
    db: DBSession,
    user: CurrentUser,
    mois: Optional[str] = None,
) -> PaiementExterneOverview:
    _require_volet(user)
    month_start = (
        _parse_mois(mois)
        if mois
        else datetime.now(timezone.utc).date().replace(day=1)
    )
    logements = (
        await db.execute(
            select(Logement).where(Logement.immeuble_id == immeuble_id)
        )
    ).scalars().all()
    rows = await _rows_externes(db, list(logements), month_start)
    return PaiementExterneOverview(
        mois=month_start.strftime("%Y-%m"),
        rows=rows,
        total_attendu=round(
            sum(r.loyer_attendu or 0.0 for r in rows), 2
        ),
        total_recu=round(sum(r.montant or 0.0 for r in rows), 2),
        nb_payes=sum(1 for r in rows if r.etat == "paye"),
        nb_impayes=sum(
            1 for r in rows if r.etat in ("partiel", "retard", "attente")
        ),
    )


# ─── Vue PORTEFEUILLE : les externes dans la page Paiements générale ───


class LoyerExterneRow(BaseModel):
    """Ligne d'un logement externe, taillée pour la page Paiements.

    Même forme que ``LoyerOverviewRow`` côté interne, moins tout ce qui
    suppose un bail et un locataire nominatif (relances, frais, solde
    cumulatif) : la perception est déléguée au gestionnaire.
    """

    immeuble_id: int
    immeuble_name: str
    logement_id: int
    logement_numero: str
    loyer_mensuel: float
    montant_paye: Optional[float] = None
    paye_le: Optional[date] = None
    etat: str
    solde_total: float = 0.0
    solde_anterieur: bool = False
    locataire_nom: Optional[str] = None


class LoyersExternesOverview(BaseModel):
    mois: str
    rows: List[LoyerExterneRow] = []
    total_attendu: float = 0.0
    total_recu: float = 0.0
    nb_payes: int = 0
    nb_retards: int = 0
    nb_attente: int = 0


@router.get("/loyers/externes", response_model=LoyersExternesOverview)
async def loyers_externes_overview(
    db: DBSession,
    user: CurrentUser,
    mois: Optional[str] = None,
    entreprise_id: Optional[int] = None,
) -> LoyersExternesOverview:
    """Tous les logements en GESTION EXTERNE pour un mois.

    Sert uniquement à l'AFFICHAGE de la page Paiements générale (retour
    Phil 2026-08-13 : « ceux-ci devraient aussi être dans la page
    paiement générale »). ``/loyers/overview`` reste volontairement
    exclusif à l'interne : relances, renouvellements et dépôts ne
    doivent pas ramasser ces immeubles.
    """
    _require_volet(user)
    month_start = (
        _parse_mois(mois)
        if mois
        else datetime.now(timezone.utc).date().replace(day=1)
    )

    imm_q = select(Immeuble).where(
        Immeuble.is_active.is_(True),
        Immeuble.gestion_externe.is_(True),
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
        return LoyersExternesOverview(mois=month_start.strftime("%Y-%m"))

    logements = list(
        (
            await db.execute(
                select(Logement).where(
                    Logement.immeuble_id.in_(list(imm_by_id.keys()))
                )
            )
        ).scalars().all()
    )
    log_by_id = {lg.id: lg for lg in logements}
    base = await _rows_externes(db, logements, month_start)

    rows: List[LoyerExterneRow] = []
    for r in base:
        # « aucun » = rien n'est attendu (vacant) : hors de la page
        # Paiements, qui liste ce qui doit rentrer ce mois-ci.
        if r.etat == "aucun":
            continue
        lg = log_by_id.get(r.logement_id)
        imm = imm_by_id.get(lg.immeuble_id) if lg else None
        if imm is None:
            continue
        rows.append(
            LoyerExterneRow(
                immeuble_id=imm.id,
                immeuble_name=imm.name,
                logement_id=r.logement_id,
                logement_numero=r.logement_numero,
                loyer_mensuel=float(r.loyer_attendu or 0),
                montant_paye=r.montant,
                paye_le=r.paye_le,
                etat=r.etat,
                solde_total=r.solde_total,
                solde_anterieur=r.solde_anterieur,
                locataire_nom=r.locataire_nom,
            )
        )
    rows.sort(
        key=lambda r: (
            _ORDRE_ETAT.get(r.etat, 9),
            r.immeuble_name,
            r.logement_numero or "",
        )
    )
    return LoyersExternesOverview(
        mois=month_start.strftime("%Y-%m"),
        rows=rows,
        total_attendu=round(sum(r.loyer_mensuel for r in rows), 2),
        total_recu=round(sum(r.montant_paye or 0.0 for r in rows), 2),
        nb_payes=sum(1 for r in rows if r.etat == "paye"),
        nb_retards=sum(
            1 for r in rows if r.etat in ("retard", "partiel")
        ),
        nb_attente=sum(1 for r in rows if r.etat == "attente"),
    )


class PaiementExterneCreate(BaseModel):
    logement_id: int
    mois: str
    montant: Optional[float] = Field(default=None, ge=0)


@router.post(
    "/paiements-externes",
    response_model=PaiementExterneRow,
    status_code=status.HTTP_201_CREATED,
)
async def marquer_paiement_externe(
    payload: PaiementExterneCreate, db: DBSession, user: CurrentUser
) -> PaiementExterneRow:
    """Enregistre un montant reçu — s'AJOUTE au cumul du mois (paiements
    partiels possibles). Sans montant : le mois est réputé payé au
    complet (loyer attendu)."""
    _require_volet(user)
    month_start = _parse_mois(payload.mois)
    lg = await db.get(Logement, payload.logement_id)
    if lg is None:
        raise HTTPException(status_code=404, detail="Logement introuvable.")
    existing = (
        await db.execute(
            select(PaiementExterne).where(
                PaiementExterne.logement_id == lg.id,
                PaiementExterne.mois_couvert == month_start,
            )
        )
    ).scalars().first()
    today = date.today()
    if existing is None:
        existing = PaiementExterne(
            logement_id=lg.id,
            mois_couvert=month_start,
            created_by_email=getattr(user, "email", None),
            created_at=datetime.now(timezone.utc),
        )
        db.add(existing)
    # Figer l'attendu du mois à la PREMIÈRE saisie (retour client
    # 2026-08-14) : un changement de loyer sur le logement après coup ne
    # doit plus réécrire les mois déjà réglés. Même hiérarchie que
    # l'affichage : loyer saisi d'abord, bail résiduel en filet.
    if existing.loyer_attendu is None:
        from app.services.loyer_effectif import loyer_effectif

        bail_actif = (
            await db.execute(
                select(Bail).where(
                    Bail.logement_id == lg.id,
                    Bail.status == BailStatus.ACTIF.value,
                )
            )
        ).scalars().first()
        attendu_courant = loyer_effectif(
            lg,
            float(bail_actif.loyer_mensuel or 0) if bail_actif else None,
            gestion_externe=True,
        )
        if attendu_courant is not None:
            existing.loyer_attendu = attendu_courant
    if payload.montant is not None:
        # Cumul du mois : un mois déjà « payé au complet » (montant
        # NULL) vaut l'attendu figé, pas 0 (bug latent 2026-09-09).
        if existing.montant is not None:
            base = float(existing.montant)
        elif existing.paye_le is not None and existing.loyer_attendu is not None:
            base = float(existing.loyer_attendu)
        else:
            base = 0.0
        existing.montant = round(base + payload.montant, 2)
    else:
        existing.montant = None  # payé au complet (= loyer attendu figé)
    existing.paye_le = today
    await db.commit()
    return PaiementExterneRow(
        logement_id=lg.id,
        logement_numero=lg.numero,
        logement_status=lg.status,
        loyer_attendu=None,
        paye=True,
        etat="paye",
        montant=(
            float(existing.montant)
            if existing.montant is not None
            else None
        ),
        paye_le=today,
    )


@router.delete(
    "/paiements-externes/{logement_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def annuler_paiement_externe(
    logement_id: int, mois: str, db: DBSession, user: CurrentUser
) -> None:
    """Erreur de saisie : le mois redevient impayé pour ce logement."""
    _require_volet(user)
    month_start = _parse_mois(mois)
    rows = (
        await db.execute(
            select(PaiementExterne).where(
                PaiementExterne.logement_id == logement_id,
                PaiementExterne.mois_couvert == month_start,
            )
        )
    ).scalars().all()
    for r in rows:
        await db.delete(r)
    await db.commit()


# ─── Factures ponctuelles ──────────────────────────────────────────────


class FactureExterneRead(BaseModel):
    id: int
    immeuble_id: int
    logement_id: Optional[int] = None
    logement_numero: Optional[str] = None
    date_facture: date
    montant: float
    fournisseur: Optional[str] = None
    description: Optional[str] = None


class FactureExterneWrite(BaseModel):
    date_facture: date
    montant: float = Field(..., gt=0)
    fournisseur: Optional[str] = Field(default=None, max_length=160)
    description: Optional[str] = None
    logement_id: Optional[int] = None


class RollupLogementFactures(BaseModel):
    logement_id: Optional[int] = None
    logement_numero: str
    total: float
    nb: int


class FacturesExternesOverview(BaseModel):
    annee: int
    rows: List[FactureExterneRead] = []
    total_annee: float = 0.0
    par_logement: List[RollupLogementFactures] = []


def _fact_read(f: FactureExterne, numero: Optional[str]) -> FactureExterneRead:
    return FactureExterneRead(
        id=f.id,
        immeuble_id=f.immeuble_id,
        logement_id=f.logement_id,
        logement_numero=numero,
        date_facture=f.date_facture,
        montant=float(f.montant or 0),
        fournisseur=f.fournisseur,
        description=f.description,
    )


@router.get(
    "/immeubles/{immeuble_id}/factures-externes",
    response_model=FacturesExternesOverview,
)
async def factures_externes_overview(
    immeuble_id: int,
    db: DBSession,
    user: CurrentUser,
    annee: Optional[int] = None,
) -> FacturesExternesOverview:
    _require_volet(user)
    annee = annee or date.today().year
    rows = (
        await db.execute(
            select(FactureExterne)
            .where(
                FactureExterne.immeuble_id == immeuble_id,
                FactureExterne.date_facture >= date(annee, 1, 1),
                FactureExterne.date_facture <= date(annee, 12, 31),
            )
            .order_by(
                FactureExterne.date_facture.desc(), FactureExterne.id.desc()
            )
        )
    ).scalars().all()
    numeros = {
        lg.id: lg.numero
        for lg in (
            await db.execute(
                select(Logement).where(Logement.immeuble_id == immeuble_id)
            )
        ).scalars().all()
    }
    rollup: dict[Optional[int], RollupLogementFactures] = {}
    total = 0.0
    for f in rows:
        m = float(f.montant or 0)
        total += m
        key = f.logement_id
        if key not in rollup:
            rollup[key] = RollupLogementFactures(
                logement_id=key,
                logement_numero=(
                    numeros.get(key, f"#{key}") if key else "Immeuble (commun)"
                ),
                total=0.0,
                nb=0,
            )
        rollup[key].total = round(rollup[key].total + m, 2)
        rollup[key].nb += 1
    return FacturesExternesOverview(
        annee=annee,
        rows=[_fact_read(f, numeros.get(f.logement_id)) for f in rows],
        total_annee=round(total, 2),
        par_logement=sorted(
            rollup.values(), key=lambda r: -r.total
        ),
    )


@router.post(
    "/immeubles/{immeuble_id}/factures-externes",
    response_model=FactureExterneRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_facture_externe(
    immeuble_id: int,
    payload: FactureExterneWrite,
    db: DBSession,
    user: CurrentUser,
) -> FactureExterneRead:
    _require_volet(user)
    numero = None
    if payload.logement_id is not None:
        lg = await db.get(Logement, payload.logement_id)
        if lg is None or lg.immeuble_id != immeuble_id:
            raise HTTPException(
                status_code=400,
                detail="Logement invalide pour cet immeuble.",
            )
        numero = lg.numero
    obj = FactureExterne(
        immeuble_id=immeuble_id,
        logement_id=payload.logement_id,
        date_facture=payload.date_facture,
        montant=payload.montant,
        fournisseur=(payload.fournisseur or "").strip() or None,
        description=(payload.description or "").strip() or None,
        created_by_email=getattr(user, "email", None),
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return _fact_read(obj, numero)


@router.put(
    "/factures-externes/{facture_id}", response_model=FactureExterneRead
)
async def update_facture_externe(
    facture_id: int,
    payload: FactureExterneWrite,
    db: DBSession,
    user: CurrentUser,
) -> FactureExterneRead:
    _require_volet(user)
    obj = await db.get(FactureExterne, facture_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    numero = None
    if payload.logement_id is not None:
        lg = await db.get(Logement, payload.logement_id)
        if lg is None or lg.immeuble_id != obj.immeuble_id:
            raise HTTPException(
                status_code=400,
                detail="Logement invalide pour cet immeuble.",
            )
        numero = lg.numero
    obj.logement_id = payload.logement_id
    obj.date_facture = payload.date_facture
    obj.montant = payload.montant
    obj.fournisseur = (payload.fournisseur or "").strip() or None
    obj.description = (payload.description or "").strip() or None
    await db.commit()
    await db.refresh(obj)
    return _fact_read(obj, numero)


@router.delete(
    "/factures-externes/{facture_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_facture_externe(
    facture_id: int, db: DBSession, user: CurrentUser
) -> None:
    _require_volet(user)
    obj = await db.get(FactureExterne, facture_id)
    if obj is not None:
        await db.delete(obj)
        await db.commit()
