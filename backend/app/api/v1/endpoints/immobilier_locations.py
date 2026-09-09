"""Pipeline « Locations » (relocation / vacances) — Gestion locative.

Un LocationDossier suit la relocation d'un logement : le locataire
confirme son départ → annonce(s) publiée(s) → visites de candidats →
candidat retenu → reloué (nouveau bail signé ailleurs). Tout est
consigné À LA MAIN par l'employé — aucun automatisme externe.

Endpoints :
    GET    /immobilier/locations/overview   (KPIs + dossiers enrichis)
    POST   /immobilier/locations            (créer un dossier)
    PATCH  /immobilier/locations/{id}
    DELETE /immobilier/locations/{id}
    POST   /immobilier/locations/{id}/annonces
    PATCH  /immobilier/locations/annonces/{id}
    DELETE /immobilier/locations/annonces/{id}
    POST   /immobilier/locations/{id}/visites
    PATCH  /immobilier/locations/visites/{id}
    DELETE /immobilier/locations/visites/{id}
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DBSession
from app.core.permissions import visible_immeuble_ids
from app.models.immobilier import (
    Bail,
    BailRenouvellement,
    BailStatus,
    Immeuble,
    Locataire,
    LocationAnnonce,
    LocationDossier,
    LocationDossierStatut,
    LocationVisite,
    Logement,
    LogementStatus,
)

from app.services.locatif_depart import (
    DOSSIER_STATUTS_REGLES,
    marquer_prise_en_charge_humaine,
    recaler_statut_logement,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/immobilier", tags=["immobilier-locations"])

#: Règle UNIQUE « dossier actif » (m1, audit 2026-08-13) : tout ce qui
#: n'est pas réglé (annulé/reloué) est actif — dérivé de l'enum pour
#: que les deux ensembles ne divergent jamais.
STATUTS_ACTIFS = {
    s.value for s in LocationDossierStatut
} - set(DOSSIER_STATUTS_REGLES)

STATUTS_VALIDES = {s.value for s in LocationDossierStatut}


def _require_volet(user: CurrentUser) -> None:
    volets = getattr(user, "volets", None)
    if volets is None or "immobilier" not in volets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Volet « Gestion immobilière » non autorisé.",
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Schemas ────────────────────────────────────────────────────────────


class AnnonceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    dossier_id: int
    plateforme: str
    url: Optional[str] = None
    publiee_le: Optional[date] = None
    active: bool


class AnnonceCreate(BaseModel):
    plateforme: str = Field(..., min_length=1, max_length=64)
    url: Optional[str] = Field(default=None, max_length=1000)
    publiee_le: Optional[date] = None


class AnnonceUpdate(BaseModel):
    plateforme: Optional[str] = Field(default=None, max_length=64)
    url: Optional[str] = Field(default=None, max_length=1000)
    publiee_le: Optional[date] = None
    active: Optional[bool] = None


class VisiteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    dossier_id: int
    quand: Optional[datetime] = None
    candidat_nom: str
    candidat_contact: Optional[str] = None  # legacy (tél/courriel mélangés)
    candidat_email: Optional[str] = None
    candidat_phone: Optional[str] = None
    statut: str
    interesse: Optional[bool] = None
    notes: Optional[str] = None
    # Prélocation : null = pas faite, true = OK, false = KO.
    enquete_credit: Optional[bool] = None
    enquete_references: Optional[bool] = None
    enquete_emploi: Optional[bool] = None
    enquete_notes: Optional[str] = None
    retenu: bool = False
    #: Candidat = locataire existant (déjà client).
    locataire_id: Optional[int] = None


class VisiteCreate(BaseModel):
    quand: Optional[datetime] = None
    #: Locataire EXISTANT — nom/courriel/téléphone repris de sa fiche.
    locataire_id: Optional[int] = None
    candidat_nom: str = Field(..., min_length=1, max_length=255)
    candidat_contact: Optional[str] = Field(default=None, max_length=255)
    candidat_email: Optional[str] = Field(default=None, max_length=320)
    candidat_phone: Optional[str] = Field(default=None, max_length=50)
    notes: Optional[str] = None


class VisiteUpdate(BaseModel):
    quand: Optional[datetime] = None
    candidat_nom: Optional[str] = Field(default=None, max_length=255)
    candidat_contact: Optional[str] = Field(default=None, max_length=255)
    candidat_email: Optional[str] = Field(default=None, max_length=320)
    candidat_phone: Optional[str] = Field(default=None, max_length=50)
    statut: Optional[str] = Field(default=None, max_length=16)
    interesse: Optional[bool] = None
    notes: Optional[str] = None
    enquete_credit: Optional[bool] = None
    enquete_references: Optional[bool] = None
    enquete_emploi: Optional[bool] = None
    enquete_notes: Optional[str] = None
    retenu: Optional[bool] = None


class DossierRow(BaseModel):
    id: int
    logement_id: int
    logement_numero: str
    immeuble_id: int
    immeuble_name: str
    bail_id: Optional[int] = None
    locataire_sortant: Optional[str] = None
    statut: str
    date_depart: Optional[date] = None
    loyer_demande: Optional[float] = None
    loyer_ancien: Optional[float] = None
    reloue_le: Optional[date] = None
    notes: Optional[str] = None
    # Dépôt de garantie du LOCATAIRE SORTANT (interconnexion Dépôts) :
    # à rendre à son départ — rappel affiché sur le dossier.
    depot_sortant: Optional[float] = None
    depot_sortant_rendu_le: Optional[date] = None
    # Bail créé par la conversion « candidat retenu → bail » (lien).
    nouveau_bail_id: Optional[int] = None
    nouveau_locataire_id: Optional[int] = None
    gestion_externe: bool = False
    annonces: List[AnnonceRead] = Field(default_factory=list)
    visites: List[VisiteRead] = Field(default_factory=list)
    created_at: Optional[datetime] = None


class LocationsOverview(BaseModel):
    rows: List[DossierRow] = Field(default_factory=list)
    nb_actifs: int = 0
    nb_annonces_actives: int = 0
    nb_visites_a_venir: int = 0
    nb_reloues_90j: int = 0
    # Jours de vacance moyens des dossiers actifs dont le locataire est
    # déjà parti (aujourd'hui − date de départ).
    jours_vacants_moyens: Optional[float] = None


class DossierCreate(BaseModel):
    # logement_id OU bail_id : avec seulement bail_id (boutons « bail non
    # prolongé » des pages Renouvellements/Locataire), le logement est
    # dérivé du bail.
    logement_id: Optional[int] = None
    bail_id: Optional[int] = None
    date_depart: Optional[date] = None
    loyer_demande: Optional[float] = Field(default=None, ge=0)
    notes: Optional[str] = None


class DossierUpdate(BaseModel):
    statut: Optional[str] = Field(default=None, max_length=24)
    date_depart: Optional[date] = None
    loyer_demande: Optional[float] = Field(default=None, ge=0)
    reloue_le: Optional[date] = None
    notes: Optional[str] = None


# ─── Helpers ────────────────────────────────────────────────────────────


async def _dossier_or_404(db, dossier_id: int) -> LocationDossier:
    obj = await db.get(LocationDossier, dossier_id)
    if obj is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Dossier de location introuvable."
        )
    return obj


async def _to_row(db, d: LocationDossier) -> DossierRow:
    lg = await db.get(Logement, d.logement_id)
    im = await db.get(Immeuble, lg.immeuble_id) if lg else None
    locataire_sortant = None
    depot_sortant = None
    depot_sortant_rendu_le = None
    if d.bail_id:
        bail = await db.get(Bail, d.bail_id)
        if bail:
            loc = await db.get(Locataire, bail.locataire_id)
            locataire_sortant = loc.full_name if loc else None
            if bail.depot_garantie is not None and float(bail.depot_garantie) > 0:
                depot_sortant = float(bail.depot_garantie)
                depot_sortant_rendu_le = bail.depot_rendu_le
    annonces = (
        await db.execute(
            select(LocationAnnonce)
            .where(LocationAnnonce.dossier_id == d.id)
            .order_by(LocationAnnonce.id.asc())
        )
    ).scalars().all()
    visites = (
        await db.execute(
            select(LocationVisite)
            .where(LocationVisite.dossier_id == d.id)
            .order_by(LocationVisite.quand.asc().nulls_last())
        )
    ).scalars().all()
    nouveau_locataire_id = None
    if d.nouveau_bail_id:
        nb = await db.get(Bail, d.nouveau_bail_id)
        nouveau_locataire_id = nb.locataire_id if nb else None
    return DossierRow(
        id=d.id,
        logement_id=d.logement_id,
        logement_numero=(lg.numero if lg else f"#{d.logement_id}"),
        immeuble_id=(im.id if im else 0),
        immeuble_name=(im.name if im else "—"),
        bail_id=d.bail_id,
        locataire_sortant=locataire_sortant,
        statut=d.statut,
        date_depart=d.date_depart,
        loyer_demande=(
            float(d.loyer_demande) if d.loyer_demande is not None else None
        ),
        loyer_ancien=(
            float(d.loyer_ancien) if d.loyer_ancien is not None else None
        ),
        reloue_le=d.reloue_le,
        notes=d.notes,
        depot_sortant=depot_sortant,
        depot_sortant_rendu_le=depot_sortant_rendu_le,
        nouveau_bail_id=d.nouveau_bail_id,
        nouveau_locataire_id=nouveau_locataire_id,
        gestion_externe=bool(im.gestion_externe) if im else False,
        annonces=[AnnonceRead.model_validate(a) for a in annonces],
        visites=[VisiteRead.model_validate(v) for v in visites],
        created_at=d.created_at,
    )


async def _regress_si_plus_de_visites(db, dossier_id: int) -> None:
    """Si le dossier est à « Visite prévue » mais qu'il ne reste AUCUNE
    visite planifiée (elles ont été faites sans retenir personne, ou
    annulées/absents/supprimées), il RECULE à « Annonce publiée » (ou
    « Départ confirmé » s'il n'y a pas d'annonce active) — demande Phil
    2026-07-10."""
    dossier = await db.get(LocationDossier, dossier_id)
    if dossier is None or dossier.statut != LocationDossierStatut.VISITES.value:
        return
    visites = (
        await db.execute(
            select(LocationVisite).where(
                LocationVisite.dossier_id == dossier_id
            )
        )
    ).scalars().all()
    if any(v.retenu for v in visites):
        return
    if any(v.statut == "planifiee" for v in visites):
        return
    annonces_actives = (
        await db.execute(
            select(LocationAnnonce).where(
                LocationAnnonce.dossier_id == dossier_id,
                LocationAnnonce.active.is_(True),
            )
        )
    ).scalars().first()
    dossier.statut = (
        LocationDossierStatut.ANNONCE_PUBLIEE.value
        if annonces_actives is not None
        else LocationDossierStatut.AVIS_RECU.value
    )
    dossier.updated_at = _now()


# ─── Overview ───────────────────────────────────────────────────────────


@router.get("/locations/overview", response_model=LocationsOverview)
async def locations_overview(
    db: DBSession,
    user: CurrentUser,
    entreprise_id: Optional[int] = None,
    immeuble_id: Optional[int] = None,
) -> LocationsOverview:
    """Tous les dossiers de relocation visibles (actifs d'abord), avec
    agrégats. Immeubles en gestion externe exclus : la relocation y
    relève du gestionnaire tiers."""
    _require_volet(user)

    imm_q = select(Immeuble).where(Immeuble.gestion_externe.isnot(True))
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
        return LocationsOverview()

    # v16 — « toutes les unités libres ont un dossier » : la CRÉATION ne
    # se fait plus ici (M9a, audit 2026-08-13 : un GET ne mute pas, et
    # les dossiers fantômes finissaient en frais facturables). Elle vit
    # dans le service ``locatif_depart.ouvrir_dossiers_unites_vacantes``,
    # appelé par les mutations qui rendent un logement vacant + au boot.

    logement_ids = [
        row[0]
        for row in (
            await db.execute(
                select(Logement.id).where(Logement.immeuble_id.in_(imm_ids))
            )
        ).all()
    ]
    if not logement_ids:
        return LocationsOverview()

    dossiers = (
        await db.execute(
            select(LocationDossier)
            .where(LocationDossier.logement_id.in_(logement_ids))
            .order_by(LocationDossier.created_at.desc())
        )
    ).scalars().all()

    rows = [await _to_row(db, d) for d in dossiers]
    # Actifs d'abord, puis complétés/annulés (déjà triés par création desc).
    rows.sort(key=lambda r: 0 if r.statut in STATUTS_ACTIFS else 1)

    now = _now()
    today = now.date()
    nb_annonces_actives = sum(
        1
        for r in rows
        if r.statut in STATUTS_ACTIFS
        for a in r.annonces
        if a.active
    )
    nb_visites_a_venir = sum(
        1
        for r in rows
        if r.statut in STATUTS_ACTIFS
        for v in r.visites
        if v.statut == "planifiee" and (v.quand is None or v.quand >= now)
    )
    # Vacance moyenne : dossiers actifs dont le locataire est déjà parti.
    vacances = [
        (today - r.date_depart).days
        for r in rows
        if r.statut in STATUTS_ACTIFS
        and r.date_depart is not None
        and r.date_depart <= today
    ]
    return LocationsOverview(
        rows=rows,
        nb_actifs=sum(1 for r in rows if r.statut in STATUTS_ACTIFS),
        nb_annonces_actives=nb_annonces_actives,
        nb_visites_a_venir=nb_visites_a_venir,
        nb_reloues_90j=sum(
            1
            for r in rows
            if r.statut == LocationDossierStatut.RELOUE.value
            and r.reloue_le is not None
            and r.reloue_le >= today - timedelta(days=90)
        ),
        jours_vacants_moyens=(
            round(sum(vacances) / len(vacances), 1) if vacances else None
        ),
    )


# ─── CRUD dossier ───────────────────────────────────────────────────────


@router.post(
    "/locations",
    response_model=DossierRow,
    status_code=status.HTTP_201_CREATED,
)
async def create_dossier(
    payload: DossierCreate, db: DBSession, user: CurrentUser
) -> DossierRow:
    """Ouvre un dossier de relocation. Si un bail sortant est fourni,
    photographie son loyer (delta affichable) et propose sa fin comme
    date de départ."""
    _require_volet(user)
    # logement_id dérivable du bail (boutons « bail non prolongé »).
    logement_id = payload.logement_id
    if logement_id is None and payload.bail_id is not None:
        b = await db.get(Bail, payload.bail_id)
        if b is not None:
            logement_id = b.logement_id
    if logement_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "logement_id ou bail_id requis.",
        )
    lg = await db.get(Logement, logement_id)
    if lg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Logement introuvable.")
    # Gestion EXTERNE : pas de relocation dans Kratos (2026-09-09).
    from app.services.gestion_externe import erreur_externe, immeuble_est_externe

    if await immeuble_est_externe(db, lg.immeuble_id):
        raise erreur_externe("pas de dossier de relocation dans Kratos.")

    # Un seul dossier ACTIF par logement — sinon on s'y perd.
    existing = (
        await db.execute(
            select(LocationDossier).where(
                LocationDossier.logement_id == logement_id,
                LocationDossier.statut.in_(list(STATUTS_ACTIFS)),
            )
        )
    ).scalars().first()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Une relocation est déjà en cours pour ce logement.",
        )

    loyer_ancien = None
    date_depart = payload.date_depart
    if payload.bail_id:
        bail = await db.get(Bail, payload.bail_id)
        if bail is not None:
            loyer_ancien = (
                float(bail.loyer_mensuel)
                if bail.loyer_mensuel is not None
                else None
            )
            if date_depart is None:
                date_depart = bail.date_fin

    obj = LocationDossier(
        logement_id=logement_id,
        bail_id=payload.bail_id,
        date_depart=date_depart,
        loyer_demande=(
            payload.loyer_demande
            if payload.loyer_demande is not None
            else (
                float(lg.loyer_demande)
                if lg.loyer_demande is not None
                else loyer_ancien
            )
        ),
        loyer_ancien=loyer_ancien,
        notes=payload.notes,
    )
    obj.created_at = _now()
    obj.updated_at = _now()
    db.add(obj)
    # Cycle unifié (2026-08-13) : « Départ » / « Non renouvelé » ne se
    # contentent plus d'ouvrir le dossier — le bail sortant se ferme à
    # la date de départ (sa fin par défaut : il reste actif jusqu'à
    # l'échéance, le recalage lazy le termine ensuite), le logement est
    # recalé et le cycle de renouvellement passe à « depart » (plus de
    # faux « réputé accepté »). Le dossier fraîchement créé est flushé
    # AVANT l'appel pour que le service le complète au lieu d'en
    # recréer un.
    if payload.bail_id:
        await db.flush()
        from app.services.locatif_depart import declarer_depart

        await declarer_depart(
            db,
            payload.bail_id,
            date_depart=date_depart,
            source="Bouton « Départ » / « Non renouvelé »",
        )
    await db.commit()
    await db.refresh(obj)
    return await _to_row(db, obj)


async def _bail_propose_orphelin(
    db, dossier: LocationDossier
) -> Optional[Bail]:
    """Bail PROPOSÉ de ce logement qui n'appartient à aucun autre
    dossier actif — donc celui de ce dossier-ci.

    Sert à réparer un lien perdu plutôt qu'à bloquer l'utilisateur. On
    reste prudent : un bail déjà revendiqué par un autre dossier n'est
    jamais volé, et sans candidat retenu il n'y a rien à rattacher.
    """
    if dossier.statut in (
        LocationDossierStatut.AVIS_RECU.value,
        LocationDossierStatut.ANNONCE_PUBLIEE.value,
        LocationDossierStatut.VISITES.value,
    ):
        return None
    revendiques = {
        r[0]
        for r in (
            await db.execute(
                select(LocationDossier.nouveau_bail_id).where(
                    LocationDossier.id != dossier.id,
                    LocationDossier.nouveau_bail_id.is_not(None),
                    LocationDossier.statut.notin_(
                        list(DOSSIER_STATUTS_REGLES)
                    ),
                )
            )
        ).all()
    }
    candidats = (
        await db.execute(
            select(Bail)
            .where(
                Bail.logement_id == dossier.logement_id,
                Bail.status == BailStatus.PROPOSE.value,
            )
            .order_by(Bail.id.desc())
        )
    ).scalars().all()
    for b in candidats:
        if b.id not in revendiques:
            return b
    return None


@router.patch("/locations/{dossier_id}", response_model=DossierRow)
async def update_dossier(
    dossier_id: int,
    payload: DossierUpdate,
    db: DBSession,
    user: CurrentUser,
) -> DossierRow:
    _require_volet(user)
    obj = await _dossier_or_404(db, dossier_id)
    data = payload.model_dump(exclude_unset=True)
    if "statut" in data and data["statut"] not in STATUTS_VALIDES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Statut invalide."
        )
    # Matrice de transitions (audit 2026-07-31) — le kanban ne peut pas
    # fabriquer d'états impossibles :
    nouveau_statut = data.get("statut")
    if nouveau_statut and nouveau_statut != obj.statut:
        if (
            nouveau_statut
            in (
                LocationDossierStatut.BAIL_A_ENVOYER.value,
                LocationDossierStatut.BAIL_ENVOYE.value,
                LocationDossierStatut.RELOUE.value,
            )
            and obj.nouveau_bail_id is None
        ):
            # RÉPARATION avant refus (audit 2026-08-19). Un aller-retour
            # sur le kanban détachait le bail créé à la conversion, mais
            # le bail, lui, survivait : le dossier se retrouvait avec un
            # candidat retenu, un bail orphelin, et ce garde-fou qui
            # répondait « convertis le candidat d'abord » — impasse dont
            # Phil ne pouvait plus sortir. Si un bail PROPOSÉ traîne sur
            # ce logement sans appartenir à un autre dossier actif, il
            # est manifestement celui de ce dossier : on le rattache.
            orphelin = await _bail_propose_orphelin(db, obj)
            if orphelin is not None:
                obj.nouveau_bail_id = orphelin.id
                log.info(
                    "Dossier %s : bail proposé %s rattaché (lien perdu "
                    "lors d'un aller-retour du kanban)",
                    obj.id, orphelin.id,
                )
            else:
                # Message ACTIONNABLE : dire ce qui manque ET où
                # cliquer. « Trop compliqué pour rien » (Phil) vient
                # souvent d'un refus qui ne dit pas quoi faire.
                a_un_candidat = (
                    await db.execute(
                        select(LocationVisite).where(
                            LocationVisite.dossier_id == obj.id,
                            LocationVisite.retenu.is_(True),
                        )
                    )
                ).scalars().first() is not None
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    (
                        "Ce dossier n'a pas encore de bail. Ouvre la "
                        "fiche et clique « Créer le locataire + bail » "
                        "sur le candidat retenu."
                        if a_un_candidat
                        else "Ce dossier n'a pas encore de candidat "
                        "retenu. Ouvre la fiche, retiens un candidat "
                        "dans « Visites & candidats », puis crée le "
                        "locataire."
                    ),
                )
        if obj.statut == LocationDossierStatut.RELOUE.value:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Ce dossier est reloué — pour revenir en arrière, "
                "passe par la résiliation du bail (page Baux).",
            )
        if (
            obj.statut == LocationDossierStatut.ANNULE.value
            and nouveau_statut in STATUTS_ACTIFS
        ):
            autre = (
                await db.execute(
                    select(LocationDossier).where(
                        LocationDossier.logement_id == obj.logement_id,
                        LocationDossier.id != obj.id,
                        LocationDossier.statut.notin_(
                            list(DOSSIER_STATUTS_REGLES)
                        ),
                    )
                )
            ).scalars().first()
            if autre is not None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Un autre dossier de relocation est déjà actif "
                    "sur ce logement.",
                )
        # m2 (audit 2026-08-13, corrigé le 2026-08-19) : reculer une
        # carte AVANT le candidat retenu détache le bail créé à la
        # conversion — à ce stade on abandonne le candidat lui-même,
        # donc le lien n'a plus de sens.
        #
        # ⚠️ « Candidat retenu » est EXCLU de cette liste. Y revenir ne
        # renie pas le candidat : il est toujours retenu, et son bail
        # existe toujours. Détacher le lien à ce moment-là fabriquait
        # exactement l'impasse rapportée par Phil — carte bloquée, et
        # « créer le locataire » proposé pour un locataire déjà créé.
        if (
            nouveau_statut
            in (
                LocationDossierStatut.AVIS_RECU.value,
                LocationDossierStatut.ANNONCE_PUBLIEE.value,
                LocationDossierStatut.VISITES.value,
            )
            and obj.nouveau_bail_id is not None
        ):
            obj.nouveau_bail_id = None
        # M9b : un humain déplace la carte au-delà de « Départ
        # confirmé » → le dossier auto-créé est pris en charge (il
        # redevient facturable comme frais de relocation).
        if nouveau_statut not in (
            LocationDossierStatut.AVIS_RECU.value,
            LocationDossierStatut.ANNULE.value,
        ):
            marquer_prise_en_charge_humaine(obj)
    for k, v in data.items():
        setattr(obj, k, v)
    # Passage à « reloué » : quand un bail a été créé pour ce dossier,
    # le bail SIGNÉ (CORPIQ) doit être importé d'abord — il devient le
    # bail au dossier partout (retour Phil 2026-07-31).
    if data.get("statut") == LocationDossierStatut.RELOUE.value:
        if obj.nouveau_bail_id is not None:
            nb = await db.get(Bail, obj.nouveau_bail_id)
            # Le blocage reste la réponse PAR DÉFAUT : dans la
            # quasi-totalité des cas, un bail sans document est un
            # oubli. Mais il se franchit — en déclarant une exception
            # motivée (POST /baux/{id}/exception-document), qui reste
            # signée et datée.
            if (
                nb is not None
                and nb.document_id is None
                and not (nb.sans_document_motif or "").strip()
            ):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "Importe d'abord le bail signé — le dossier "
                    "passera à « Reloué » automatiquement. S'il n'y a "
                    "vraiment aucun bail à joindre, déclare une "
                    "exception motivée.",
                )
        if obj.reloue_le is None:
            obj.reloue_le = _now().date()
        # Règle UNIQUE du statut du logement (M2, audit 2026-08-13) :
        # le service recalcule d'après les baux — un bail qui commence
        # dans 3 mois donne « réservé », pas « occupé » de force.
        await recaler_statut_logement(db, obj.logement_id)
        lg = await db.get(Logement, obj.logement_id)
        if lg is not None:
            # Miroir « loyer demandé » (2026-08-13) : reloué → le
            # logement suit le loyer réel du NOUVEAU bail.
            if obj.nouveau_bail_id is not None:
                nb_sync = await db.get(Bail, obj.nouveau_bail_id)
                if (
                    nb_sync is not None
                    and nb_sync.loyer_mensuel is not None
                ):
                    lg.loyer_demande = nb_sync.loyer_mensuel
    # M4 (audit 2026-08-13) : le prix affiché modifié dans le kanban
    # redescend sur la fiche du logement pendant la vacance — dossier
    # ACTIF + logement VACANT → Logement.loyer_demande suit le dossier.
    if (
        "loyer_demande" in data
        and obj.loyer_demande is not None
        and obj.statut not in DOSSIER_STATUTS_REGLES
    ):
        lg_m4 = await db.get(Logement, obj.logement_id)
        if (
            lg_m4 is not None
            and lg_m4.status == LogementStatus.VACANT.value
        ):
            lg_m4.loyer_demande = obj.loyer_demande
            lg_m4.updated_at = _now()
    obj.updated_at = _now()
    await db.commit()
    await db.refresh(obj)
    return await _to_row(db, obj)


@router.delete(
    "/locations/{dossier_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_dossier(
    dossier_id: int, db: DBSession, user: CurrentUser
) -> None:
    """Supprime le dossier — ET le locataire/bail créés à la
    conversion s'il y en a. Un dossier RELOUÉ (bail signé en
    production) ne supprime QUE la carte : le bail et le locataire
    restent intacts (audit 2026-07-31)."""
    _require_volet(user)
    obj = await _dossier_or_404(db, dossier_id)
    logement_id = obj.logement_id
    if obj.statut != LocationDossierStatut.RELOUE.value:
        await _supprimer_bail_et_locataire_crees(db, obj)
    await db.delete(obj)
    await db.flush()
    await _recaler_statut_logement(db, logement_id)
    await db.commit()


# ─── Conversion candidat retenu → locataire + bail ──────────────────────


class ConvertirRequest(BaseModel):
    """Tout est prérempli côté UI mais MODIFIABLE avant confirmation —
    rien ne se crée sans l'accord explicite de l'usager."""

    #: Locataire EXISTANT (déjà client) — si fourni, AUCUNE fiche n'est
    #: créée : le bail s'attache à sa fiche (pas de doublon).
    locataire_id: Optional[int] = None
    locataire_nom: str = Field(..., min_length=2, max_length=255)
    locataire_email: Optional[str] = Field(default=None, max_length=320)
    locataire_phone: Optional[str] = Field(default=None, max_length=50)
    date_naissance: Optional[date] = None
    nas_last4: Optional[str] = Field(default=None, max_length=4)
    ancienne_adresse: Optional[str] = Field(default=None, max_length=500)
    employeur: Optional[str] = Field(default=None, max_length=255)
    revenu_annuel: Optional[float] = Field(default=None, ge=0)
    date_debut: date
    date_fin: date
    loyer_mensuel: float = Field(..., ge=0)
    depot_garantie: Optional[float] = Field(default=None, ge=0)


class ConvertirResult(BaseModel):
    locataire_id: int
    bail_id: int
    immeuble_id: int


@router.post(
    "/locations/{dossier_id}/convertir",
    response_model=ConvertirResult,
    status_code=status.HTTP_201_CREATED,
)
async def convertir_dossier(
    dossier_id: int,
    payload: ConvertirRequest,
    db: DBSession,
    user: CurrentUser,
) -> ConvertirResult:
    """Le candidat retenu devient LOCATAIRE avec son BAIL (statut
    « proposé »). Le dossier passe à « bail à envoyer » : le bail se
    prépare et s'envoie dans le système de la CORPIQ (hors Kratos),
    puis la carte se glisse à « bail envoyé ». Le passage à « reloué »
    exige l'IMPORT du bail signé. Le logement devient « réservé »
    (occupé quand le bail commencera)."""
    _require_volet(user)
    # Verrou de ligne (audit 2026-07-31) : deux conversions simultanées
    # (double-clic) deviennent séquentielles — la seconde frappe la
    # garde au lieu de créer un doublon.
    dossier = (
        await db.execute(
            select(LocationDossier)
            .where(LocationDossier.id == dossier_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if dossier is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Dossier introuvable."
        )
    if (
        dossier.statut
        in (
            LocationDossierStatut.RELOUE.value,
            LocationDossierStatut.BAIL_ENVOYE.value,
            LocationDossierStatut.BAIL_A_ENVOYER.value,
            LocationDossierStatut.ANNULE.value,
        )
        or dossier.nouveau_bail_id is not None
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Ce dossier est annulé ou un locataire a déjà été créé.",
        )
    # Candidat retenu déjà LIÉ à un locataire existant → sa fiche est
    # réutilisée même sans choisir le mode « existant » (zéro doublon).
    locataire_id_effectif = payload.locataire_id
    if locataire_id_effectif is None:
        locataire_id_effectif = (
            await db.execute(
                select(LocationVisite.locataire_id).where(
                    LocationVisite.dossier_id == dossier.id,
                    LocationVisite.retenu.is_(True),
                    LocationVisite.locataire_id.is_not(None),
                )
            )
        ).scalars().first()
    # Identité obligatoire seulement à la CRÉATION d'une fiche — un
    # locataire existant garde la sienne.
    if locataire_id_effectif is None:
        if payload.date_naissance is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "La date de naissance est obligatoire.",
            )
        if not (payload.locataire_email or "").strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Le courriel du locataire est obligatoire.",
            )
        if not (payload.locataire_phone or "").strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Le téléphone du locataire est obligatoire.",
            )
    if payload.date_fin <= payload.date_debut:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "La fin du bail doit être après son début.",
        )
    lg = await db.get(Logement, dossier.logement_id)
    if lg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Logement introuvable.")

    now = _now()

    # Transfert de la PRÉLOCATION : le candidat retenu emporte ses
    # résultats d'enquête et ses notes dans la fiche du locataire.
    retenu = (
        await db.execute(
            select(LocationVisite).where(
                LocationVisite.dossier_id == dossier.id,
                LocationVisite.retenu.is_(True),
            )
        )
    ).scalars().first()
    notes_locataire = None
    if retenu is not None:
        def _lbl(v: Optional[bool]) -> str:
            return "OK" if v is True else "refusé" if v is False else "non faite"

        lignes = [
            "Prélocation (dossier de relocation) :",
            f"- Enquête de crédit : {_lbl(retenu.enquete_credit)}",
            f"- Références : {_lbl(retenu.enquete_references)}",
            f"- Vérification d'emploi : {_lbl(retenu.enquete_emploi)}",
        ]
        if (retenu.enquete_notes or "").strip():
            lignes.append(f"Notes d'enquête : {retenu.enquete_notes.strip()}")
        if (retenu.notes or "").strip():
            lignes.append(f"Notes du candidat : {retenu.notes.strip()}")
        notes_locataire = "\n".join(lignes)

    if locataire_id_effectif is not None:
        # Déjà client : on réutilise SA fiche (historique conservé,
        # pas de doublon) — la trace d'enquête s'ajoute à ses notes.
        locataire = await db.get(Locataire, locataire_id_effectif)
        if locataire is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Locataire existant introuvable.",
            )
        if notes_locataire:
            locataire.notes = (
                (locataire.notes + "\n\n") if locataire.notes else ""
            ) + notes_locataire
        locataire.updated_at = now
    else:
        locataire = Locataire(
            full_name=payload.locataire_nom.strip(),
            email=(payload.locataire_email or "").strip() or None,
            phone=(payload.locataire_phone or "").strip() or None,
            date_naissance=payload.date_naissance,
            nas_last4=(payload.nas_last4 or "").strip() or None,
            ancienne_adresse=(payload.ancienne_adresse or "").strip()
            or None,
            employeur=(payload.employeur or "").strip() or None,
            revenu_annuel=payload.revenu_annuel,
            notes=notes_locataire,
        )
        locataire.created_at = now
        locataire.updated_at = now
        db.add(locataire)
        await db.flush()
        dossier.locataire_cree = True

    bail = Bail(
        logement_id=dossier.logement_id,
        locataire_id=locataire.id,
        date_debut=payload.date_debut,
        date_fin=payload.date_fin,
        loyer_mensuel=payload.loyer_mensuel,
        depot_garantie=payload.depot_garantie,
        status="propose",
        # « Louer indéfiniment (chambre) » : le logement impose le bail
        # AU MOIS — loyer figé, aucun renouvellement (Phil 2026-08-13).
        au_mois=(
            True
            if getattr(lg, "location_en_chambres", False)
            else None
        ),
    )
    bail.created_at = now
    bail.updated_at = now
    db.add(bail)
    await db.flush()

    dossier.nouveau_bail_id = bail.id
    dossier.updated_at = now
    # Réservé = bail signé/à signer pas encore commencé (le passage à
    # « occupé » suit le cycle normal du bail).
    lg.status = LogementStatus.RESERVE.value
    immeuble_id = lg.immeuble_id

    # Le bail se prépare et s'envoie dans le système CORPIQ (hors
    # Kratos) : la carte passe à « bail à envoyer ».
    dossier.statut = LocationDossierStatut.BAIL_A_ENVOYER.value
    # M9b : la conversion est un geste HUMAIN — un dossier auto-créé
    # est pris en charge (frais de relocation à nouveau facturables).
    marquer_prise_en_charge_humaine(dossier)

    locataire_id = locataire.id
    bail_id = bail.id
    await db.commit()

    # Consentement aux communications électroniques : le PDF est généré et
    # ARCHIVÉ au dossier du bail. Aucun courriel n'est envoyé — l'envoi pour
    # signature reste un geste manuel (règle « zéro envoi auto au locataire »).
    # Best-effort : un échec ne bloque jamais la conversion.
    try:
        from app.api.v1.endpoints.immobilier_extras import (
            preparer_consentement_communications,
        )

        await preparer_consentement_communications(db, bail_id, user)
    except Exception:  # noqa: BLE001 — la conversion prime
        log.exception(
            "Préparation du consentement communications échouée (bail %s)", bail_id
        )

    return ConvertirResult(
        locataire_id=locataire_id,
        bail_id=bail_id,
        immeuble_id=immeuble_id,
    )


# ─── Désistement du candidat converti ───────────────────────────────────


async def _recaler_statut_logement(db, logement_id: int) -> None:
    """Statut du logement recalculé d'après ses baux — délégué à la
    règle UNIQUE du service (M2/M3, audit 2026-08-13 : occupé/réservé/
    vacant, baux au mois et hors-location respectés)."""
    await recaler_statut_logement(db, logement_id)


async def _supprimer_bail_et_locataire_crees(db, dossier) -> None:
    """Supprime le bail créé à la conversion — et le locataire s'il n'a
    aucun autre bail (avec ses communications et documents). Utilisé par
    le DÉSISTEMENT et par la SUPPRESSION du dossier (retour Phil
    2026-07-31 : « delete-le aussi »). 409 si des paiements existent."""
    from app.models.immobilier import (
        BailRenouvellement,
        ImmDocument,
        LocataireCommunication,
        PaiementLoyer,
    )

    if dossier.nouveau_bail_id is None:
        return
    bail = await db.get(Bail, dossier.nouveau_bail_id)
    # Garde-fou (audit 2026-07-31) : un bail SIGNÉ ou ACTIF ne se rase
    # jamais par un désistement/une suppression de carte — c'est la
    # RÉSILIATION qui gère la vraie fin de bail (historique conservé).
    if bail is not None and (
        bail.status != "propose"
        or bail.document_id is not None
        or bail.signed_at is not None
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Ce bail est signé ou actif — utilise « Mettre fin au "
            "bail » (page Baux) au lieu du désistement.",
        )
    locataire_id = bail.locataire_id if bail else None
    if bail is not None:
        paiements = (
            await db.execute(
                select(PaiementLoyer).where(
                    PaiementLoyer.bail_id == bail.id
                )
            )
        ).scalars().all()
        if paiements:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Des paiements sont déjà enregistrés sur ce bail — "
                "retire-les d'abord.",
            )
        for doc in (
            await db.execute(
                select(ImmDocument).where(ImmDocument.bail_id == bail.id)
            )
        ).scalars().all():
            await db.delete(doc)
        for r in (
            await db.execute(
                select(BailRenouvellement).where(
                    BailRenouvellement.bail_id == bail.id
                )
            )
        ).scalars().all():
            await db.delete(r)
        await db.delete(bail)
    # La fiche d'un client EXISTANT réutilisée à la conversion n'est
    # JAMAIS supprimée — seules les fiches créées par la conversion
    # partent avec le désistement (audit 2026-07-31).
    if locataire_id is not None and not getattr(
        dossier, "locataire_cree", False
    ):
        locataire_id = None
    if locataire_id is not None:
        autres = (
            await db.execute(
                select(Bail).where(
                    Bail.locataire_id == locataire_id,
                    Bail.id != dossier.nouveau_bail_id,
                )
            )
        ).scalars().all()
        if not autres:
            lo = await db.get(Locataire, locataire_id)
            if lo is not None:
                for c in (
                    await db.execute(
                        select(LocataireCommunication).where(
                            LocataireCommunication.locataire_id
                            == locataire_id
                        )
                    )
                ).scalars().all():
                    await db.delete(c)
                for doc in (
                    await db.execute(
                        select(ImmDocument).where(
                            ImmDocument.locataire_id == locataire_id
                        )
                    )
                ).scalars().all():
                    await db.delete(doc)
                await db.delete(lo)
    dossier.nouveau_bail_id = None


@router.post(
    "/locations/{dossier_id}/desistement", response_model=DossierRow
)
async def desistement_candidat(
    dossier_id: int, db: DBSession, user: CurrentUser
) -> DossierRow:
    """Le candidat converti se désiste : le bail créé (et le locataire,
    s'il n'a aucun autre bail) est SUPPRIMÉ, le dossier revient à
    « Départ confirmé » et le logement reprend son vrai statut (occupé
    si le locataire sortant est encore en place, vacant sinon)."""
    _require_volet(user)
    dossier = await _dossier_or_404(db, dossier_id)
    if dossier.nouveau_bail_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Aucun locataire converti sur ce dossier.",
        )
    await _supprimer_bail_et_locataire_crees(db, dossier)
    dossier.reloue_le = None
    dossier.statut = LocationDossierStatut.AVIS_RECU.value
    # m4 (audit 2026-08-13) : le dossier repart de « Départ confirmé »
    # — ses infos de départ sont restaurées depuis le bail SORTANT
    # s'il existe (fill-only : on ne touche pas une saisie manuelle).
    if dossier.bail_id:
        bail_sortant = await db.get(Bail, dossier.bail_id)
        if bail_sortant is not None:
            if dossier.date_depart is None:
                dossier.date_depart = bail_sortant.date_fin
            loyer_sortant = (
                float(bail_sortant.loyer_mensuel)
                if bail_sortant.loyer_mensuel is not None
                else None
            )
            if dossier.loyer_ancien is None:
                dossier.loyer_ancien = loyer_sortant
            if dossier.loyer_demande is None:
                dossier.loyer_demande = loyer_sortant
    dossier.updated_at = _now()
    await db.flush()
    await recaler_statut_logement(db, dossier.logement_id)
    await db.commit()
    obj = await _dossier_or_404(db, dossier_id)
    return await _to_row(db, obj)


# ─── Page « Baux » (split de Baux & paiements, v15) ─────────────────────


class SuiviBailRow(BaseModel):
    logement_id: int
    logement_numero: str
    logement_status: str
    #: « Louer indéfiniment (chambre) » — loyer figé, bail au mois,
    #: jamais de renouvellement (Logement.location_en_chambres).
    logement_en_chambres: bool = False
    immeuble_id: int
    immeuble_name: str
    # Bail COURANT (actif) — None = ligne grise (à créer / importer).
    bail_id: Optional[int] = None
    locataire_id: Optional[int] = None
    locataire_nom: Optional[str] = None
    date_debut: Optional[date] = None
    date_fin: Optional[date] = None
    loyer_mensuel: Optional[float] = None
    au_mois: Optional[bool] = None
    #: Jour d'échéance du loyer (bail TAL « Ou le ___ ») — affiché
    #: « payable le X » à côté du loyer quand ce n'est pas le 1er.
    jour_echeance: Optional[int] = None
    document_id: Optional[int] = None
    signed_at: Optional[datetime] = None
    # Prochain bail (proposé / futur) — suivi même signé à l'externe.
    prochain_bail_id: Optional[int] = None
    prochain_locataire_id: Optional[int] = None
    prochain_locataire_nom: Optional[str] = None
    prochain_date_debut: Optional[date] = None
    prochain_loyer: Optional[float] = None
    prochain_document_id: Optional[int] = None
    prochain_statut: Optional[str] = None  # bail_a_envoyer|bail_envoye|a_venir
    # Dossier de relocation ACTIF du logement (sélecteur de statut
    # kanban sur la page Baux — même donnée que le kanban).
    dossier_id: Optional[int] = None
    dossier_statut: Optional[str] = None
    # Entente de résiliation envoyée, en attente de signature → ligne
    # ROUGE sur la page Baux.
    #: Motif d'EXCEPTION « aucun bail à joindre », s'il a été déclaré.
    #: Sert au bouton d'exception, qui vit là où le bail vit — pas dans
    #: l'alerte (règle Phil 2026-08-19).
    sans_document_motif: Optional[str] = None
    resiliation_en_cours: bool = False
    #: SUIVI de l'entente de résiliation en attente (retour Phil
    #: 2026-08-19 : « si c'est pas signé il est pas encore mis fin »).
    #: Une entente envoyée mais jamais ouverte n'a pas le même sens
    #: qu'une entente ouverte et laissée sans réponse.
    resiliation_document_id: Optional[int] = None
    resiliation_envoye_le: Optional[datetime] = None
    resiliation_ouvert_le: Optional[datetime] = None
    resiliation_date: Optional[date] = None
    # Dernier avis de renouvellement du bail actif (pastille + bouton
    # « Avis » sur la page Baux).
    renouvellement_status: Optional[str] = None
    renouvellement_avis_document_id: Optional[int] = None


@router.get("/suivi-baux", response_model=List[SuiviBailRow])
async def suivi_baux(
    db: DBSession,
    user: CurrentUser,
    immeuble_id: Optional[int] = None,
    logement_id: Optional[int] = None,
    locataire_id: Optional[int] = None,
) -> List[SuiviBailRow]:
    """Une ligne par LOGEMENT : verte quand un bail actif est au dossier,
    grise quand il n'y a aucun bail (créer / importer). Le bail se signe
    à l'externe (CORPIQ) — le SUIVI vit ici.

    ``logement_id`` et ``locataire_id`` restreignent la MÊME donnée à une
    fiche. Retour Phil 2026-08-19 : « la section des baux d'une fiche
    doit être exactement pareille que dans la page Baux, mais juste pour
    ce locataire-là ». Un filtre, pas une deuxième implémentation —
    sinon les deux vues divergent, et c'est celle qu'on regarde le moins
    qui ment."""
    _require_volet(user)
    today = _now().date()
    imm_q = select(Immeuble).where(
        Immeuble.is_active.is_(True),
        Immeuble.gestion_externe.isnot(True),
    )
    if immeuble_id is not None:
        imm_q = imm_q.where(Immeuble.id == immeuble_id)
    immeubles = {i.id: i for i in (await db.execute(imm_q)).scalars().all()}
    visible = await visible_immeuble_ids(db, user)
    if visible is not None:
        immeubles = {k: v for k, v in immeubles.items() if k in visible}
    if not immeubles:
        return []
    logements = (
        await db.execute(
            select(Logement).where(
                Logement.immeuble_id.in_(list(immeubles.keys()))
            )
        )
    ).scalars().all()
    log_ids = [lg.id for lg in logements]
    baux = (
        await db.execute(
            select(Bail)
            .where(Bail.logement_id.in_(log_ids))
            .order_by(Bail.date_debut.asc())
        )
    ).scalars().all() if log_ids else []

    # Reconduction tacite AUTOMATIQUE (lazy, 2026-08-13) : les baux
    # échus sans réponse s'étirent d'un cycle, ceux dont le départ
    # était annoncé se terminent — la page Baux montre l'état recalé.
    from app.services.locatif_depart import reconduire_tacitement_baux_echus

    await reconduire_tacitement_baux_echus(db, baux)

    loc_ids = {b.locataire_id for b in baux if b.locataire_id}
    locs = {}
    if loc_ids:
        for lo in (
            await db.execute(
                select(Locataire).where(Locataire.id.in_(list(loc_ids)))
            )
        ).scalars().all():
            locs[lo.id] = lo
    reloc_by_bail: dict = {}
    bail_ids = [b.id for b in baux]
    if bail_ids:
        for dsr in (
            await db.execute(
                select(LocationDossier).where(
                    LocationDossier.nouveau_bail_id.in_(bail_ids),
                    LocationDossier.statut.notin_(
                        list(DOSSIER_STATUTS_REGLES)
                    ),
                )
            )
        ).scalars().all():
            reloc_by_bail[dsr.nouveau_bail_id] = dsr.statut
    # Dossier ACTIF par logement (sélecteur de statut, page Baux).
    dossier_par_log: dict = {}
    for dsr in (
        await db.execute(
            select(LocationDossier).where(
                LocationDossier.logement_id.in_(log_ids),
                LocationDossier.statut.notin_(
                    list(DOSSIER_STATUTS_REGLES)
                ),
            )
        )
    ).scalars().all() if log_ids else []:
        dossier_par_log[dsr.logement_id] = dsr
    # Ententes de résiliation en attente de signature (ligne rouge).
    resil_by_bail: dict = {}
    if bail_ids:
        import json as _json

        from app.models.immobilier import ImmDocument

        for dr in (
            await db.execute(
                select(ImmDocument).where(
                    ImmDocument.bail_id.in_(bail_ids),
                    ImmDocument.type == "avis_resiliation",
                    ImmDocument.signed_at.is_(None),
                    ImmDocument.envoye_le.is_not(None),
                )
            )
        ).scalars().all():
            try:
                dfin = _json.loads(dr.params_json or "{}").get("date_fin")
            except Exception:  # noqa: BLE001
                dfin = None
            resil_by_bail[dr.bail_id] = (dfin, dr)

    # Dernier avis de renouvellement par bail (même sémantique que
    # Suivis annuels : avis_envoye_le le plus récent, ex-æquo par id).
    last_ren_by_bail: dict = {}
    if bail_ids:
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

    actifs_par_log: dict = {}
    prochains_par_log: dict = {}
    for b in baux:
        if b.status == "actif" and b.date_debut and b.date_debut <= today:
            cur = actifs_par_log.get(b.logement_id)
            # Le plus récent des actifs commencés.
            if cur is None or (b.date_debut > cur.date_debut):
                actifs_par_log[b.logement_id] = b
        elif b.status == "propose" or (
            b.status == "actif" and b.date_debut and b.date_debut > today
        ):
            cur = prochains_par_log.get(b.logement_id)
            if cur is None or (b.date_debut < cur.date_debut):
                prochains_par_log[b.logement_id] = b

    out: List[SuiviBailRow] = []
    for lg in logements:
        im = immeubles.get(lg.immeuble_id)
        b = actifs_par_log.get(lg.id)
        p = prochains_par_log.get(lg.id)
        lo = locs.get(b.locataire_id) if b else None
        plo = locs.get(p.locataire_id) if p else None
        out.append(
            SuiviBailRow(
                logement_id=lg.id,
                logement_numero=str(lg.numero or ""),
                logement_status=lg.status,
                logement_en_chambres=bool(
                    getattr(lg, "location_en_chambres", False)
                ),
                immeuble_id=im.id if im else 0,
                immeuble_name=im.name if im else "—",
                bail_id=b.id if b else None,
                locataire_id=lo.id if lo else None,
                locataire_nom=lo.full_name if lo else None,
                date_debut=b.date_debut if b else None,
                date_fin=b.date_fin if b else None,
                loyer_mensuel=(
                    float(b.loyer_mensuel) if b and b.loyer_mensuel else None
                ),
                au_mois=b.au_mois if b else None,
                jour_echeance=(b.jour_echeance or 1) if b else None,
                document_id=b.document_id if b else None,
                signed_at=b.signed_at if b else None,
                prochain_bail_id=p.id if p else None,
                prochain_locataire_id=plo.id if plo else None,
                prochain_locataire_nom=plo.full_name if plo else None,
                prochain_date_debut=p.date_debut if p else None,
                prochain_loyer=(
                    float(p.loyer_mensuel) if p and p.loyer_mensuel else None
                ),
                prochain_document_id=p.document_id if p else None,
                prochain_statut=(
                    reloc_by_bail.get(p.id, "a_venir") if p else None
                ),
                dossier_id=(
                    dossier_par_log[lg.id].id
                    if lg.id in dossier_par_log
                    else None
                ),
                dossier_statut=(
                    dossier_par_log[lg.id].statut
                    if lg.id in dossier_par_log
                    else None
                ),
                sans_document_motif=(
                    getattr(b, "sans_document_motif", None) if b else None
                ),
                resiliation_en_cours=bool(b and b.id in resil_by_bail),
                resiliation_document_id=(
                    resil_by_bail[b.id][1].id
                    if b and b.id in resil_by_bail else None
                ),
                resiliation_envoye_le=(
                    resil_by_bail[b.id][1].envoye_le
                    if b and b.id in resil_by_bail else None
                ),
                resiliation_ouvert_le=(
                    resil_by_bail[b.id][1].ouvert_le
                    if b and b.id in resil_by_bail else None
                ),
                resiliation_date=(
                    date.fromisoformat(resil_by_bail[b.id][0])
                    if b and resil_by_bail.get(b.id)
                    and resil_by_bail[b.id][0]
                    else None
                ),
                renouvellement_status=(
                    last_ren_by_bail[b.id].status
                    if b and b.id in last_ren_by_bail
                    else None
                ),
                renouvellement_avis_document_id=(
                    last_ren_by_bail[b.id].document_id
                    if b and b.id in last_ren_by_bail
                    else None
                ),
            )
        )
    out.sort(
        key=lambda r: (
            r.bail_id is not None,
            r.immeuble_name,
            r.logement_numero,
        )
    )
    if logement_id is not None:
        out = [r for r in out if r.logement_id == logement_id]
    if locataire_id is not None:
        # Le locataire d'AUJOURD'HUI ou celui qui arrive : les deux
        # concernent sa fiche.
        out = [
            r for r in out
            if r.locataire_id == locataire_id
            or r.prochain_locataire_id == locataire_id
        ]
    return out


# ─── Annonces ───────────────────────────────────────────────────────────


@router.post(
    "/locations/{dossier_id}/annonces",
    response_model=AnnonceRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_annonce(
    dossier_id: int,
    payload: AnnonceCreate,
    db: DBSession,
    user: CurrentUser,
) -> AnnonceRead:
    _require_volet(user)
    dossier = await _dossier_or_404(db, dossier_id)
    obj = LocationAnnonce(
        dossier_id=dossier.id,
        plateforme=payload.plateforme.strip(),
        url=(payload.url or "").strip() or None,
        publiee_le=payload.publiee_le or _now().date(),
    )
    obj.created_at = _now()
    obj.updated_at = _now()
    db.add(obj)
    # Première annonce sur un dossier « départ confirmé » → il passe
    # naturellement à « annonce publiée ».
    if dossier.statut == LocationDossierStatut.AVIS_RECU.value:
        dossier.statut = LocationDossierStatut.ANNONCE_PUBLIEE.value
        dossier.updated_at = _now()
    # M9b : publier une annonce est un geste HUMAIN — un dossier
    # auto-créé est pris en charge (frais de relocation facturables).
    marquer_prise_en_charge_humaine(dossier)
    await db.commit()
    await db.refresh(obj)
    return AnnonceRead.model_validate(obj)


@router.patch("/locations/annonces/{annonce_id}", response_model=AnnonceRead)
async def update_annonce(
    annonce_id: int,
    payload: AnnonceUpdate,
    db: DBSession,
    user: CurrentUser,
) -> AnnonceRead:
    _require_volet(user)
    obj = await db.get(LocationAnnonce, annonce_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Annonce introuvable.")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    obj.updated_at = _now()
    await db.commit()
    await db.refresh(obj)
    return AnnonceRead.model_validate(obj)


@router.delete(
    "/locations/annonces/{annonce_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_annonce(
    annonce_id: int, db: DBSession, user: CurrentUser
) -> None:
    _require_volet(user)
    obj = await db.get(LocationAnnonce, annonce_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Annonce introuvable.")
    await db.delete(obj)
    await db.commit()


# ─── Visites ────────────────────────────────────────────────────────────


@router.post(
    "/locations/{dossier_id}/visites",
    response_model=VisiteRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_visite(
    dossier_id: int,
    payload: VisiteCreate,
    db: DBSession,
    user: CurrentUser,
) -> VisiteRead:
    _require_volet(user)
    dossier = await _dossier_or_404(db, dossier_id)
    lo = None
    if payload.locataire_id is not None:
        lo = await db.get(Locataire, payload.locataire_id)
        if lo is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Locataire existant introuvable.",
            )
    obj = LocationVisite(
        dossier_id=dossier.id,
        quand=payload.quand,
        locataire_id=payload.locataire_id,
        candidat_nom=payload.candidat_nom.strip(),
        candidat_contact=(payload.candidat_contact or "").strip() or None,
        candidat_email=(payload.candidat_email or "").strip()
        or (lo.email if lo else None),
        candidat_phone=(payload.candidat_phone or "").strip()
        or (lo.phone if lo else None),
        notes=payload.notes,
    )
    obj.created_at = _now()
    obj.updated_at = _now()
    db.add(obj)
    # Première visite → le dossier passe à « visites en cours ».
    if dossier.statut in (
        LocationDossierStatut.AVIS_RECU.value,
        LocationDossierStatut.ANNONCE_PUBLIEE.value,
    ):
        dossier.statut = LocationDossierStatut.VISITES.value
        dossier.updated_at = _now()
    # M9b : planifier une visite est un geste HUMAIN — un dossier
    # auto-créé est pris en charge (frais de relocation facturables).
    marquer_prise_en_charge_humaine(dossier)
    await db.commit()
    await db.refresh(obj)
    return VisiteRead.model_validate(obj)


@router.patch("/locations/visites/{visite_id}", response_model=VisiteRead)
async def update_visite(
    visite_id: int,
    payload: VisiteUpdate,
    db: DBSession,
    user: CurrentUser,
) -> VisiteRead:
    _require_volet(user)
    obj = await db.get(LocationVisite, visite_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Visite introuvable.")
    data = payload.model_dump(exclude_unset=True)
    if "statut" in data and data["statut"] not in (
        "planifiee", "faite", "absent", "annulee"
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Statut de visite invalide."
        )
    for k, v in data.items():
        setattr(obj, k, v)
    # « Retenu » est EXCLUSIF par dossier : retenir ce candidat déretient
    # les autres et fait avancer le dossier à « candidat retenu ».
    if data.get("retenu") is True:
        autres = (
            await db.execute(
                select(LocationVisite).where(
                    LocationVisite.dossier_id == obj.dossier_id,
                    LocationVisite.id != obj.id,
                    LocationVisite.retenu.is_(True),
                )
            )
        ).scalars().all()
        for a in autres:
            a.retenu = False
        dossier = await db.get(LocationDossier, obj.dossier_id)
        if dossier is not None and dossier.statut in STATUTS_ACTIFS:
            dossier.statut = LocationDossierStatut.CANDIDAT_RETENU.value
            dossier.updated_at = _now()
    # Une visite qui n'aboutit pas (faite/absent/annulée sans retenir) et
    # plus rien de planifié → le dossier recule à « Annonce publiée ».
    if data.get("statut") in ("faite", "absent", "annulee") or (
        data.get("retenu") is False
    ):
        await _regress_si_plus_de_visites(db, obj.dossier_id)
    obj.updated_at = _now()
    await db.commit()
    await db.refresh(obj)
    return VisiteRead.model_validate(obj)


@router.delete(
    "/locations/visites/{visite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_visite(
    visite_id: int, db: DBSession, user: CurrentUser
) -> None:
    _require_volet(user)
    obj = await db.get(LocationVisite, visite_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Visite introuvable.")
    dossier_id = obj.dossier_id
    await db.delete(obj)
    await db.flush()
    await _regress_si_plus_de_visites(db, dossier_id)
    await db.commit()
