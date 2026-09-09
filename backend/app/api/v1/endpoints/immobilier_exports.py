"""Exports du pôle Gestion locative — tableaux CSV/Excel + zip de documents.

    GET /immobilier/exports/paiements?mois=YYYY-MM[&du=&au=][&immeuble_id=][&fmt=]
    GET /immobilier/exports/locataires[?immeuble_id=][&fmt=]
    GET /immobilier/exports/baux[?immeuble_id=][&fmt=]
    GET /immobilier/exports/logements[?immeuble_id=][&fmt=]
    GET /immobilier/exports/immeubles[?immeuble_id=][&entreprise_id=][&fmt=]
    GET /immobilier/exports/depots[?immeuble_id=][&fmt=]

    GET /immobilier/locataires/{id}/documents.zip?categorie=dossier|tout
    GET /immobilier/baux/{id}/documents.zip?categorie=dossier|tout
    GET /immobilier/logements/{id}/documents.zip?categorie=dossier|tout
    GET /immobilier/immeubles/{id}/documents.zip?categorie=dossier|tout

Règle d'or : AUCUN deuxième calcul. Chaque tableau réutilise la fonction
interne de l'endpoint qui alimente déjà la page correspondante
(``/loyers/overview`` + ``/loyers/externes`` pour Paiements,
``/suivi-baux`` pour Baux, ``/depots/overview`` pour Dépôts…) — l'export
est le reflet exact de ce que l'écran affiche, filtres compris.

Le zip charge les PDF PAR LOTS (``undefer`` explicite : ``pdf_blob``
est différé partout ailleurs) et refuse au-delà de MAX_DOCS documents ou
MAX_ZIP_BYTES octets (413 explicite, jamais un timeout silencieux).
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, List, Optional, Sequence

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.api.deps import CurrentUser, DBSession
from app.core.permissions import visible_immeuble_ids
from app.models.immobilier import (
    Bail,
    ImmDocument,
    Immeuble,
    Locataire,
    Logement,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/immobilier", tags=["immobilier-exports"])

#: Garde-fous du zip : au-delà, 413 explicite (le navigateur n'attend
#: pas un téléchargement de 30 minutes qui finit en timeout Render).
MAX_DOCS = 300
MAX_ZIP_BYTES = 200 * 1024 * 1024
#: Taille des lots de PDF chargés en mémoire (les blobs font jusqu'à
#: 20 Mo chacun — voir _MAX_UPLOAD dans immobilier_documents).
_LOT_BLOBS = 20
#: Plage maximale d'un export de paiements sur période (mois).
_MAX_MOIS_PERIODE = 36

_FORMATS = {"csv", "xlsx"}

_ETAT_PAIEMENT = {
    "paye": "Payé",
    "partiel": "Partiel",
    "retard": "En retard",
    "attente": "En attente",
    "vacant": "Vacant",
    "aucun": "—",
}
_STATUT_DEPOT = {
    "detenu": "Détenu",
    "a_rendre": "À rendre",
    "rendu": "Rendu",
    "aucun": "Aucun dépôt saisi",
}
_STATUT_LOGEMENT = {
    "occupe": "Occupé",
    "vacant": "Vacant",
    "reserve": "Réservé",
    "hors_location": "Hors location",
}


def _require_volet(user: CurrentUser) -> None:
    volets = getattr(user, "volets", None)
    if volets is None or "immobilier" not in volets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Volet « Gestion immobilière » non autorisé.",
        )


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ── Helper unique : tableau → CSV / XLSX ───────────────────────────────


def _cellule_csv(v: Any) -> str:
    """Valeur → texte CSV. Booléens en français, dates ISO, montants à
    deux décimales (point décimal — lisible partout, y compris par un
    import « Données → À partir d'un texte » d'Excel)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "oui" if v else "non"
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc)
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _cellule_xlsx(v: Any) -> Any:
    """Valeur → cellule openpyxl. Les nombres restent des nombres (Excel
    sait sommer une colonne), les datetimes perdent leur fuseau (openpyxl
    refuse les tz-aware), les booléens s'écrivent en français."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "oui" if v else "non"
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v
    return v


def _csv_bytes(rows: Sequence[Sequence[Any]], colonnes: Sequence[str]) -> bytes:
    """CSV UTF-8 avec BOM (Excel reconnaît l'encodage) et « ; » comme
    séparateur (convention Excel en français)."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(list(colonnes))
    for r in rows:
        w.writerow([_cellule_csv(v) for v in r])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _xlsx_bytes(
    rows: Sequence[Sequence[Any]], colonnes: Sequence[str], titre: str
) -> bytes:
    """Classeur openpyxl en mode write_only (pas de modèle en mémoire
    pour des milliers de lignes), en-têtes en gras."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title=titre[:31] or "Export")
    gras = Font(bold=True)
    entetes = []
    for h in colonnes:
        c = WriteOnlyCell(ws, value=h)
        c.font = gras
        entetes.append(c)
    ws.append(entetes)
    for r in rows:
        ws.append([_cellule_xlsx(v) for v in r])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _table_response(
    rows: Sequence[Sequence[Any]],
    colonnes: Sequence[str],
    nom_fichier: str,
    fmt: str,
) -> Response:
    """Réponse téléchargeable ``kratos_<nom_fichier>_<AAAA-MM-JJ>.<ext>``.

    ``fmt`` = ``csv`` (BOM + « ; », UTF-8) ou ``xlsx`` (openpyxl)."""
    fmt = (fmt or "csv").lower()
    if fmt not in _FORMATS:
        raise HTTPException(
            status_code=400, detail="Format attendu : fmt=csv ou fmt=xlsx."
        )
    sujet = re.sub(r"[^A-Za-z0-9_-]+", "-", nom_fichier).strip("-") or "export"
    base = f"kratos_{sujet}_{_today().isoformat()}"
    if fmt == "xlsx":
        content = _xlsx_bytes(rows, colonnes, sujet)
        media = (
            "application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet"
        )
        fname = f"{base}.xlsx"
    else:
        content = _csv_bytes(rows, colonnes)
        media = "text/csv; charset=utf-8"
        fname = f"{base}.csv"
    return Response(
        content=content,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store",
        },
    )


# ── Paiements (bail × mois + logement externe × mois) ──────────────────


def _parse_mois(mois: str) -> date:
    try:
        return datetime.strptime(mois + "-01", "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="Format mois attendu : YYYY-MM."
        )


def _mois_suivant(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def _plage_mois(
    mois: Optional[str], du: Optional[str], au: Optional[str]
) -> List[str]:
    """Liste des mois « YYYY-MM » à exporter : ``du``/``au`` (inclus)
    si les deux sont là, sinon ``mois``, sinon le mois courant."""
    if du or au:
        if not (du and au):
            raise HTTPException(
                status_code=400,
                detail="Une période demande « du » ET « au » (YYYY-MM).",
            )
        debut, fin = _parse_mois(du), _parse_mois(au)
        if fin < debut:
            raise HTTPException(
                status_code=400, detail="« au » doit être après « du »."
            )
        out: List[str] = []
        cur = debut
        while cur <= fin:
            out.append(cur.strftime("%Y-%m"))
            if len(out) > _MAX_MOIS_PERIODE:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Période trop longue (max {_MAX_MOIS_PERIODE} "
                        "mois par export)."
                    ),
                )
            cur = _mois_suivant(cur)
        return out
    if mois:
        return [_parse_mois(mois).strftime("%Y-%m")]
    return [_today().strftime("%Y-%m")]


_COLONNES_PAIEMENTS = [
    "Mois",
    "Immeuble",
    "Logement",
    "Locataire",
    "Courriel",
    "Téléphone",
    "Gestion externe",
    "Loyer attendu",
    "Jour d'échéance",
    "Frais du mois",
    "Payé",
    "Payé le",
    "Solde du mois",
    "Solde total",
    "État",
    "TAL ouvert le",
    "Nb relances",
]


@router.get("/exports/paiements")
async def export_paiements(
    db: DBSession,
    user: CurrentUser,
    mois: Optional[str] = None,
    du: Optional[str] = None,
    au: Optional[str] = None,
    immeuble_id: Optional[int] = None,
    entreprise_id: Optional[int] = None,
    fmt: str = "csv",
) -> Response:
    """Une ligne par bail × mois (interne) + une par logement × mois
    (gestion externe). Mêmes lignes, mêmes états, mêmes soldes que la
    page Paiements : on appelle ``loyers_overview`` et
    ``loyers_externes_overview`` tels quels, mois par mois."""
    _require_volet(user)
    from app.api.v1.endpoints.immobilier import loyers_overview
    from app.api.v1.endpoints.immobilier_gestion_externe import (
        loyers_externes_overview,
    )

    plage = _plage_mois(mois, du, au)
    rows: List[List[Any]] = []
    for m in plage:
        ov = await loyers_overview(
            db, user, mois=m, entreprise_id=entreprise_id
        )
        for r in ov.rows:
            if r.etat == "vacant" or not r.bail_id:
                continue  # ligne informative, pas un bail
            if immeuble_id is not None and r.immeuble_id != immeuble_id:
                continue
            frais = round(sum(float(f.montant) for f in r.frais_mois), 2)
            du_mois = round(float(r.loyer_mensuel) + frais, 2)
            paye = float(r.montant_paye or 0.0)
            rows.append(
                [
                    m,
                    r.immeuble_name,
                    r.logement_numero,
                    r.locataire_name,
                    r.locataire_email,
                    r.locataire_phone,
                    False,
                    float(r.loyer_mensuel),
                    r.jour_echeance,
                    frais,
                    r.montant_paye,
                    r.paye_le,
                    round(max(0.0, du_mois - paye), 2),
                    float(r.solde_total),
                    _ETAT_PAIEMENT.get(r.etat, r.etat),
                    r.tal_dossier_ouvert_le,
                    r.nb_relances,
                ]
            )
        ext = await loyers_externes_overview(
            db, user, mois=m, entreprise_id=entreprise_id
        )
        for r in ext.rows:
            if immeuble_id is not None and r.immeuble_id != immeuble_id:
                continue
            rows.append(
                [
                    m,
                    r.immeuble_name,
                    r.logement_numero,
                    None,
                    None,
                    None,
                    True,
                    float(r.loyer_mensuel),
                    None,
                    0.0,
                    r.montant_paye,
                    r.paye_le,
                    float(r.solde_total),
                    float(r.solde_total),
                    _ETAT_PAIEMENT.get(r.etat, r.etat),
                    None,
                    None,
                ]
            )
    suffixe = plage[0] if len(plage) == 1 else f"{plage[0]}_au_{plage[-1]}"
    return _table_response(
        rows, _COLONNES_PAIEMENTS, f"paiements_{suffixe}", fmt
    )


# ── Locataires ─────────────────────────────────────────────────────────


_COLONNES_LOCATAIRES = [
    "ID",
    "Nom",
    "Courriel",
    "Téléphone",
    "Immeuble",
    "Logement",
    "Date de naissance",
    "Employeur",
    "Revenu annuel",
    "Ancienne adresse",
    "Score de paiement",
    "Assurance confirmée le",
    "DPA statut",
    "DPA envoyé le",
    "DPA signé le",
    "Notes",
    "Créé le",
]


@router.get("/exports/locataires")
async def export_locataires(
    db: DBSession,
    user: CurrentUser,
    immeuble_id: Optional[int] = None,
    fmt: str = "csv",
) -> Response:
    """Même liste que la page Locataires (``list_locataires``) : le
    logement/immeuble est celui du bail actif le plus récent."""
    _require_volet(user)
    from app.api.v1.endpoints.immobilier import list_locataires

    items = await list_locataires(db, user, search=None)
    if immeuble_id is not None:
        items = [i for i in items if i.immeuble_id == immeuble_id]
    dpa: dict[int, tuple] = {}
    if items:
        for lid, statut, env, sig in (
            await db.execute(
                select(
                    Locataire.id,
                    Locataire.dpa_statut,
                    Locataire.dpa_envoye_le,
                    Locataire.dpa_signe_le,
                ).where(Locataire.id.in_([i.id for i in items]))
            )
        ).all():
            dpa[lid] = (statut, env, sig)
    rows: List[List[Any]] = []
    for i in items:
        statut, env, sig = dpa.get(i.id, (None, None, None))
        rows.append(
            [
                i.id,
                i.full_name,
                i.email,
                i.phone,
                i.immeuble_name,
                i.logement_numero,
                i.date_naissance,
                i.employeur,
                i.revenu_annuel,
                i.ancienne_adresse,
                i.paiement_score,
                i.assurance_confirmee_le,
                statut,
                env,
                sig,
                i.notes,
                i.created_at,
            ]
        )
    return _table_response(rows, _COLONNES_LOCATAIRES, "locataires", fmt)


# ── Baux ───────────────────────────────────────────────────────────────


_COLONNES_BAUX = [
    "Immeuble",
    "Logement",
    "Statut logement",
    "Louer indéfiniment (chambre)",
    "Bail ID",
    "Locataire",
    "Début",
    "Fin",
    "Loyer mensuel",
    "Au mois",
    "Jour d'échéance",
    "Dépôt de garantie",
    "Chauffage inclus",
    "Eau chaude incluse",
    "Électricité incluse",
    "Internet inclus",
    "Signé le",
    "PDF au dossier",
    "Motif sans document",
    "TAL ouvert le",
    "Renouvellement",
    "Résiliation en cours",
    "Résiliation le",
    "Dossier relocation",
    "Prochain locataire",
    "Prochain début",
    "Prochain loyer",
    "Prochain statut",
]


@router.get("/exports/baux")
async def export_baux(
    db: DBSession,
    user: CurrentUser,
    immeuble_id: Optional[int] = None,
    fmt: str = "csv",
) -> Response:
    """Une ligne par logement, bail courant + prochain — la page Baux
    (``suivi_baux``), enrichie des champs du bail (dépôt, inclusions,
    TAL) qui ne s'affichent pas dans le tableau."""
    _require_volet(user)
    from app.api.v1.endpoints.immobilier_locations import suivi_baux

    lignes = await suivi_baux(db, user, immeuble_id=immeuble_id)
    baux: dict[int, Bail] = {}
    ids = [l.bail_id for l in lignes if l.bail_id]
    if ids:
        for b in (
            await db.execute(select(Bail).where(Bail.id.in_(ids)))
        ).scalars().all():
            baux[b.id] = b
    rows: List[List[Any]] = []
    for l in lignes:
        b = baux.get(l.bail_id) if l.bail_id else None
        rows.append(
            [
                l.immeuble_name,
                l.logement_numero,
                _STATUT_LOGEMENT.get(l.logement_status, l.logement_status),
                l.logement_en_chambres,
                l.bail_id,
                l.locataire_nom,
                l.date_debut,
                l.date_fin,
                l.loyer_mensuel,
                l.au_mois,
                l.jour_echeance,
                (float(b.depot_garantie) if b and b.depot_garantie else None),
                b.chauffage_inclus if b else None,
                b.eau_chaude_inclus if b else None,
                b.electricite_inclus if b else None,
                b.internet_inclus if b else None,
                l.signed_at,
                (l.document_id is not None) if l.bail_id else None,
                l.sans_document_motif,
                b.tal_dossier_ouvert_le if b else None,
                l.renouvellement_status,
                l.resiliation_en_cours,
                l.resiliation_date,
                l.dossier_statut,
                l.prochain_locataire_nom,
                l.prochain_date_debut,
                l.prochain_loyer,
                l.prochain_statut,
            ]
        )
    return _table_response(rows, _COLONNES_BAUX, "baux", fmt)


# ── Logements ──────────────────────────────────────────────────────────


_COLONNES_LOGEMENTS = [
    "Immeuble",
    "Numéro",
    "Statut",
    "Libre le",
    "Type",
    "Étage",
    "Pièces",
    "Chambres",
    "Salles de bain",
    "Louer indéfiniment (chambre)",
    "Loyer demandé",
    "Loyer actuel (bail)",
    "Notes",
    "Logement ID",
    "Immeuble ID",
]


@router.get("/exports/logements")
async def export_logements(
    db: DBSession,
    user: CurrentUser,
    immeuble_id: Optional[int] = None,
    entreprise_id: Optional[int] = None,
    fmt: str = "csv",
) -> Response:
    """Tous les logements des immeubles actifs visibles — même hiérarchie
    du loyer effectif que la page Logements (``list_logements``)."""
    _require_volet(user)
    from app.api.v1.endpoints.immobilier import list_immeubles, list_logements

    immeubles = await list_immeubles(
        db, user, only_active=True, entreprise_id=entreprise_id
    )
    if immeuble_id is not None:
        immeubles = [i for i in immeubles if i.id == immeuble_id]
    rows: List[List[Any]] = []
    for imm in immeubles:
        for lg in await list_logements(imm.id, db, user):
            rows.append(
                [
                    imm.name,
                    lg.numero,
                    _STATUT_LOGEMENT.get(lg.status, lg.status),
                    lg.libre_le,
                    lg.type,
                    lg.etage,
                    lg.nb_pieces_decimal,
                    lg.nb_chambres,
                    lg.nb_sdb,
                    lg.location_en_chambres,
                    lg.loyer_demande,
                    lg.loyer_actuel,
                    lg.notes,
                    lg.id,
                    imm.id,
                ]
            )
    return _table_response(rows, _COLONNES_LOGEMENTS, "logements", fmt)


# ── Immeubles ──────────────────────────────────────────────────────────


_COLONNES_IMMEUBLES = [
    "ID",
    "Nom",
    "Adresse",
    "Ville",
    "Code postal",
    "Type",
    "Année de construction",
    "Nb logements",
    "Logements actifs",
    "Logements occupés",
    "Taux d'occupation",
    "Revenu mensuel",
    "Prix d'achat",
    "Date d'achat",
    "Matricule",
    "Superficie terrain",
    "Superficie bâtiment",
    "Gestion externe",
    "Gestionnaire externe",
    "Contact gestionnaire",
    "Actif",
]


@router.get("/exports/immeubles")
async def export_immeubles(
    db: DBSession,
    user: CurrentUser,
    immeuble_id: Optional[int] = None,
    entreprise_id: Optional[int] = None,
    only_active: bool = True,
    fmt: str = "csv",
) -> Response:
    """La liste de la page Immeubles (``list_immeubles`` : agrégats
    logements + revenu), complétée des champs de la fiche."""
    _require_volet(user)
    from app.api.v1.endpoints.immobilier import list_immeubles

    items = await list_immeubles(
        db, user, only_active=only_active, entreprise_id=entreprise_id
    )
    if immeuble_id is not None:
        items = [i for i in items if i.id == immeuble_id]
    detail: dict[int, Immeuble] = {}
    if items:
        for imm in (
            await db.execute(
                select(Immeuble).where(Immeuble.id.in_([i.id for i in items]))
            )
        ).scalars().all():
            detail[imm.id] = imm
    rows: List[List[Any]] = []
    for i in items:
        d = detail.get(i.id)
        rows.append(
            [
                i.id,
                i.name,
                i.address,
                i.city,
                d.postal_code if d else None,
                i.type,
                d.annee_construction if d else None,
                i.nb_logements,
                i.nb_logements_actifs,
                i.nb_logements_occupes,
                round(float(i.taux_occupation) * 100, 1),
                float(i.revenu_mensuel),
                (float(d.purchase_price) if d and d.purchase_price else None),
                d.purchase_date if d else None,
                d.matricule if d else None,
                d.superficie_terrain if d else None,
                d.superficie_batiment if d else None,
                bool(d.gestion_externe) if d else False,
                d.gestionnaire_externe_nom if d else None,
                d.gestionnaire_externe_contact if d else None,
                i.is_active,
            ]
        )
    return _table_response(rows, _COLONNES_IMMEUBLES, "immeubles", fmt)


# ── Dépôts de garantie ─────────────────────────────────────────────────


_COLONNES_DEPOTS = [
    "Immeuble",
    "Logement",
    "Locataire",
    "Montant",
    "Statut",
    "Reçu le",
    "Détenteur",
    "Rendu le",
    "Bail début",
    "Bail fin",
    "Bail ID",
]


@router.get("/exports/depots")
async def export_depots(
    db: DBSession,
    user: CurrentUser,
    immeuble_id: Optional[int] = None,
    entreprise_id: Optional[int] = None,
    fmt: str = "csv",
) -> Response:
    """La page Dépôts telle quelle (``depots_overview``)."""
    _require_volet(user)
    from app.api.v1.endpoints.immobilier import depots_overview

    ov = await depots_overview(
        db, user, entreprise_id=entreprise_id, immeuble_id=immeuble_id
    )
    rows = [
        [
            r.immeuble_name,
            r.logement_numero,
            r.locataire_name,
            float(r.montant),
            _STATUT_DEPOT.get(r.statut, r.statut),
            r.depot_recu_le,
            r.depot_detenteur,
            r.depot_rendu_le,
            r.date_debut,
            r.date_fin,
            r.bail_id,
        ]
        for r in ov.rows
    ]
    return _table_response(rows, _COLONNES_DEPOTS, "depots", fmt)


# ── Zip de documents ───────────────────────────────────────────────────


_INTERDITS_NOM = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _nom_sur(s: Optional[str], max_len: int = 80) -> str:
    """Segment de chemin sûr dans un zip (accents conservés, caractères
    interdits et espaces → « _ »)."""
    s = _INTERDITS_NOM.sub("_", (s or "").strip())
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"_+", "_", s).strip("._")
    return s[:max_len] or "sans_titre"


def _extension(d: ImmDocument, blob: bytes) -> str:
    """L'import accepte PDF/JPG/PNG tels quels : on déduit l'extension du
    contenu, puis du nom d'origine, sinon .pdf."""
    if blob[:5] == b"%PDF-":
        return ".pdf"
    if blob[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    fn = (getattr(d, "filename", None) or "").lower()
    for ext in (".pdf", ".jpg", ".jpeg", ".png"):
        if fn.endswith(ext):
            return ext
    return ".pdf"


def _nom_fichier_doc(d: ImmDocument, ext: str) -> str:
    quand = d.created_at.date().isoformat() if d.created_at else "sans-date"
    return f"{quand}_{_nom_sur(d.type, 40)}_{_nom_sur(d.titre, 60)}_{d.id}{ext}"


async def _docs_sans_blob(db, cond) -> List[ImmDocument]:
    """Métadonnées seulement (``pdf_blob`` reste différé)."""
    return list(
        (
            await db.execute(
                select(ImmDocument)
                .where(cond)
                .order_by(ImmDocument.created_at.asc(), ImmDocument.id.asc())
            )
        ).scalars().all()
    )


def _filtrer_categorie(docs: List[ImmDocument], categorie: str) -> List[ImmDocument]:
    if categorie not in ("dossier", "tout"):
        raise HTTPException(
            status_code=400, detail="categorie attendue : dossier ou tout."
        )
    if categorie == "dossier":
        from app.api.v1.endpoints.immobilier_documents import _est_dossier

        return [d for d in docs if _est_dossier(d)]
    return docs


async def _libelles_baux(db, bail_ids: set[int]) -> dict[int, str]:
    """Nom de sous-dossier par bail : ``bail_<début>_<logement>_<locataire>_<id>``."""
    if not bail_ids:
        return {}
    baux = (
        await db.execute(select(Bail).where(Bail.id.in_(list(bail_ids))))
    ).scalars().all()
    log_ids = {b.logement_id for b in baux if b.logement_id}
    loc_ids = {b.locataire_id for b in baux if b.locataire_id}
    logs: dict[int, Logement] = {}
    locs: dict[int, Locataire] = {}
    if log_ids:
        for lg in (
            await db.execute(
                select(Logement).where(Logement.id.in_(list(log_ids)))
            )
        ).scalars().all():
            logs[lg.id] = lg
    if loc_ids:
        for lo in (
            await db.execute(
                select(Locataire).where(Locataire.id.in_(list(loc_ids)))
            )
        ).scalars().all():
            locs[lo.id] = lo
    out: dict[int, str] = {}
    for b in baux:
        lg = logs.get(b.logement_id)
        lo = locs.get(b.locataire_id)
        debut = b.date_debut.isoformat() if b.date_debut else "sans-date"
        out[b.id] = "bail_" + "_".join(
            [
                debut,
                _nom_sur(f"logement_{lg.numero}" if lg else "logement", 30),
                _nom_sur(lo.full_name if lo else "locataire", 40),
                str(b.id),
            ]
        )
    return out


async def _zip_response(
    db,
    docs: List[ImmDocument],
    *,
    nom_zip: str,
    dossier_pour: Callable[[ImmDocument], str],
) -> Response:
    """Construit le zip (ZIP_DEFLATED, en mémoire) : blobs par lots avec
    ``undefer``, index.csv à la racine. 413 au-delà des garde-fous."""
    if not docs:
        raise HTTPException(
            status_code=404, detail="Aucun document à exporter."
        )
    if len(docs) > MAX_DOCS:
        raise HTTPException(
            status_code=413,  # Content Too Large
            detail=(
                f"Trop de documents pour un seul zip ({len(docs)} > "
                f"{MAX_DOCS}). Exporte par bail ou par logement."
            ),
        )
    par_id = {d.id: d for d in docs}
    index_rows: List[List[Any]] = []
    total = 0
    buf = io.BytesIO()
    noms_pris: set[str] = set()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        ordre = [d.id for d in docs]
        for i in range(0, len(ordre), _LOT_BLOBS):
            lot = ordre[i : i + _LOT_BLOBS]
            charges = (
                await db.execute(
                    select(ImmDocument)
                    .options(undefer(ImmDocument.pdf_blob))
                    .where(ImmDocument.id.in_(lot))
                )
            ).scalars().all()
            charges_par_id = {d.id: d for d in charges}
            for did in lot:
                d = charges_par_id.get(did)
                if d is None:
                    continue  # supprimé entre-temps
                blob = d.pdf_blob or b""
                if not blob:
                    continue  # ligne sans PDF (import raté) : on saute
                total += len(blob)
                if total > MAX_ZIP_BYTES:
                    raise HTTPException(
                        status_code=413,  # Content Too Large
                        detail=(
                            "Zip trop volumineux (plus de "
                            f"{MAX_ZIP_BYTES // (1024 * 1024)} Mo). "
                            "Exporte par bail ou par logement."
                        ),
                    )
                dossier = dossier_pour(d)
                chemin = _nom_fichier_doc(d, _extension(d, blob))
                if dossier:
                    chemin = f"{dossier}/{chemin}"
                # Deux documents ne peuvent pas partager un chemin (l'id
                # est dans le nom, mais on reste défensif).
                while chemin in noms_pris:
                    chemin = chemin.rsplit(".", 1)[0] + "_bis." + chemin.rsplit(".", 1)[1]
                noms_pris.add(chemin)
                zf.writestr(chemin, blob)
                index_rows.append(
                    [
                        d.created_at,
                        d.type,
                        d.titre,
                        getattr(d, "source", "genere") or "genere",
                        d.envoye_le,
                        d.envoye_a,
                        d.ouvert_le,
                        d.signed_at,
                        d.signed_by_name,
                        chemin,
                    ]
                )
                # Libère le blob de la session (mémoire) une fois écrit.
                db.expire(d, ["pdf_blob"])
        zf.writestr(
            "index.csv",
            _csv_bytes(
                index_rows,
                [
                    "Date",
                    "Type",
                    "Titre",
                    "Source",
                    "Envoyé le",
                    "Envoyé à",
                    "Ouvert le",
                    "Signé le",
                    "Signé par",
                    "Fichier",
                ],
            ),
        )
    if not index_rows:
        raise HTTPException(
            status_code=404, detail="Aucun document à exporter."
        )
    fname = f"kratos_documents_{_nom_sur(nom_zip, 60)}_{_today().isoformat()}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/baux/{bail_id}/documents.zip")
async def zip_bail_documents(
    bail_id: int,
    db: DBSession,
    user: CurrentUser,
    categorie: str = "tout",
) -> Response:
    """Tous les documents d'un bail, à plat."""
    _require_volet(user)
    if await db.get(Bail, bail_id) is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    docs = _filtrer_categorie(
        await _docs_sans_blob(db, ImmDocument.bail_id == bail_id), categorie
    )
    return await _zip_response(
        db, docs, nom_zip=f"bail_{bail_id}", dossier_pour=lambda d: ""
    )


@router.get("/locataires/{locataire_id}/documents.zip")
async def zip_locataire_documents(
    locataire_id: int,
    db: DBSession,
    user: CurrentUser,
    categorie: str = "tout",
) -> Response:
    """Documents du locataire (les siens à la racine) + ceux de ses baux
    (un sous-dossier par bail) — même périmètre que
    ``list_locataire_documents``."""
    _require_volet(user)
    loc = await db.get(Locataire, locataire_id)
    if loc is None:
        raise HTTPException(status_code=404, detail="Locataire introuvable.")
    bail_ids = {
        r[0]
        for r in (
            await db.execute(
                select(Bail.id).where(Bail.locataire_id == locataire_id)
            )
        ).all()
    }
    cond = ImmDocument.locataire_id == locataire_id
    if bail_ids:
        cond = cond | ImmDocument.bail_id.in_(list(bail_ids))
    docs = _filtrer_categorie(await _docs_sans_blob(db, cond), categorie)
    libelles = await _libelles_baux(db, {d.bail_id for d in docs if d.bail_id})
    return await _zip_response(
        db,
        docs,
        nom_zip=f"locataire_{_nom_sur(loc.full_name, 40)}_{locataire_id}",
        dossier_pour=lambda d: libelles.get(d.bail_id, "") if d.bail_id else "",
    )


@router.get("/logements/{logement_id}/documents.zip")
async def zip_logement_documents(
    logement_id: int,
    db: DBSession,
    user: CurrentUser,
    categorie: str = "tout",
) -> Response:
    """Documents importés sur le logement (racine) + ceux de tous ses
    baux, passés et actifs (un sous-dossier par bail)."""
    _require_volet(user)
    lg = await db.get(Logement, logement_id)
    if lg is None:
        raise HTTPException(status_code=404, detail="Logement introuvable.")
    bail_ids = {
        r[0]
        for r in (
            await db.execute(
                select(Bail.id).where(Bail.logement_id == logement_id)
            )
        ).all()
    }
    cond = ImmDocument.logement_id == logement_id
    if bail_ids:
        cond = cond | ImmDocument.bail_id.in_(list(bail_ids))
    docs = _filtrer_categorie(await _docs_sans_blob(db, cond), categorie)
    libelles = await _libelles_baux(db, {d.bail_id for d in docs if d.bail_id})
    return await _zip_response(
        db,
        docs,
        nom_zip=f"logement_{_nom_sur(lg.numero, 30)}_{logement_id}",
        dossier_pour=lambda d: libelles.get(d.bail_id, "") if d.bail_id else "",
    )


@router.get("/immeubles/{immeuble_id}/documents.zip")
async def zip_immeuble_documents(
    immeuble_id: int,
    db: DBSession,
    user: CurrentUser,
    categorie: str = "tout",
) -> Response:
    """Tout l'immeuble : ses documents (racine) + ceux de ses logements
    (``logement_<n°>/``), de leurs baux (``logement_<n°>/bail_…/``) et
    des locataires de ces baux (``locataire_<nom>_<id>/``)."""
    _require_volet(user)
    imm = await db.get(Immeuble, immeuble_id)
    if imm is None:
        raise HTTPException(status_code=404, detail="Immeuble introuvable.")
    visible = await visible_immeuble_ids(db, user)
    if visible is not None and immeuble_id not in visible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Immeuble non autorisé pour cet utilisateur.",
        )
    logements = (
        await db.execute(
            select(Logement).where(Logement.immeuble_id == immeuble_id)
        )
    ).scalars().all()
    log_par_id = {lg.id: lg for lg in logements}
    baux = (
        await db.execute(
            select(Bail).where(Bail.logement_id.in_(list(log_par_id.keys())))
        )
    ).scalars().all() if log_par_id else []
    bail_par_id = {b.id: b for b in baux}
    loc_ids = {b.locataire_id for b in baux if b.locataire_id}
    cond = ImmDocument.immeuble_id == immeuble_id
    if log_par_id:
        cond = cond | ImmDocument.logement_id.in_(list(log_par_id.keys()))
    if bail_par_id:
        cond = cond | ImmDocument.bail_id.in_(list(bail_par_id.keys()))
    if loc_ids:
        cond = cond | ImmDocument.locataire_id.in_(list(loc_ids))
    docs = _filtrer_categorie(await _docs_sans_blob(db, cond), categorie)
    libelles = await _libelles_baux(db, {d.bail_id for d in docs if d.bail_id})
    locs: dict[int, Locataire] = {}
    if loc_ids:
        for lo in (
            await db.execute(
                select(Locataire).where(Locataire.id.in_(list(loc_ids)))
            )
        ).scalars().all():
            locs[lo.id] = lo

    def _dossier(d: ImmDocument) -> str:
        if d.bail_id and d.bail_id in bail_par_id:
            b = bail_par_id[d.bail_id]
            lg = log_par_id.get(b.logement_id)
            racine = f"logement_{_nom_sur(lg.numero, 30)}" if lg else ""
            sous = libelles.get(d.bail_id, f"bail_{d.bail_id}")
            return f"{racine}/{sous}" if racine else sous
        if d.logement_id and d.logement_id in log_par_id:
            return f"logement_{_nom_sur(log_par_id[d.logement_id].numero, 30)}"
        if d.locataire_id and d.locataire_id in locs:
            lo = locs[d.locataire_id]
            return f"locataire_{_nom_sur(lo.full_name, 40)}_{lo.id}"
        return ""

    return await _zip_response(
        db,
        docs,
        nom_zip=f"immeuble_{_nom_sur(imm.name, 40)}_{immeuble_id}",
        dossier_pour=_dossier,
    )
