"""Documents locatifs conservés (avis TAL, trousse bail…).

    GET    /immobilier/baux/{bail_id}/documents
    GET    /immobilier/locataires/{locataire_id}/documents
    GET    /immobilier/documents/{id}/pdf
    DELETE /immobilier/documents/{id}
    POST   /immobilier/documents/{id}/envoyer-signature

Chaque génération (Générer ▾) enregistre le PDF + ses
paramètres dans ``imm_documents`` ; l'envoi pour signature crée un lien
public tokenisé /document/{token} (page publique + preuve d'ouverture +
signature — voir public_document.py).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.api.deps import CurrentUser, DBSession
from app.integrations.email_graph import EmailAttachment, get_mailer
from app.models.immobilier import (
    Bail,
    ImmDocument,
    Locataire,
    LocataireCommunication,
)
from app.services.public_links import public_base
from app.services.tal_forms import SIGNATURE_NON_REQUISE

log = logging.getLogger(__name__)

router = APIRouter(prefix="/immobilier", tags=["immobilier-documents"])


def _require_volet(user: CurrentUser) -> None:
    volets = getattr(user, "volets", None)
    if volets is None or "immobilier" not in volets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Volet « Gestion immobilière » non autorisé.",
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: Types de documents NORMALISÉS proposés dans les menus d'import
#: (retour Phil 2026-09-09 : à la création d'un locataire, le gestionnaire
#: veut déposer PLUSIEURS pièces — règlements de l'immeuble, assurance…,
#: pas seulement le bail). Miroir TS : ``IMM_DOC_TYPES`` dans
#: frontend/src/components/immobilier/doc-types.ts — garder les deux
#: listes alignées. ``/documents/import`` accepte toujours un ``type``
#: hors liste (rétro-compat : releve31, dpa, avis générés…) : la liste
#: sert aux menus, pas à valider.
IMM_DOC_TYPES: list[tuple[str, str]] = [
    ("bail", "Bail"),
    ("reglement_immeuble", "Règlements de l'immeuble"),
    ("assurance", "Preuve d'assurance"),
    ("enquete_credit", "Enquête de crédit / références"),
    ("piece_identite", "Pièce d'identité"),
    ("autre", "Autre"),
]
IMM_DOC_TYPE_LABELS: dict[str, str] = dict(IMM_DOC_TYPES)


class DocumentRead(BaseModel):
    id: int
    bail_id: Optional[int]
    locataire_id: Optional[int]
    logement_id: Optional[int] = None
    type: str
    titre: str
    params: dict = {}
    created_at: Optional[datetime] = None
    envoye_le: Optional[datetime] = None
    envoye_a: Optional[str] = None
    ouvert_le: Optional[datetime] = None
    signed_at: Optional[datetime] = None
    signed_by_name: Optional[str] = None
    #: 'genere' | 'importe' — l'importé est toujours une pièce au dossier.
    source: str = "genere"
    filename: Optional[str] = None
    #: Document remplacé par celui-ci (version précédente d'un bail,
    #: d'un avis de renouvellement…).
    remplace_document_id: Optional[int] = None
    #: False pour les simples communications (avis d'accès, rappel de
    #: paiement…) — elles vivent dans le journal, pas au dossier.
    signature_requise: bool = True


def _est_dossier(d: ImmDocument) -> bool:
    """Pièce du DOSSIER = importée à la main, ou signée/à signer.
    Les modèles sans signature (rappel de paiement, avis d'accès…) sont
    des communications : elles n'encombrent pas la section Documents."""
    if (getattr(d, "source", "genere") or "genere") == "importe":
        return True
    return d.type not in SIGNATURE_NON_REQUISE


def _doc_read(d: ImmDocument) -> DocumentRead:
    params: dict = {}
    if d.params_json:
        try:
            parsed = json.loads(d.params_json)
            if isinstance(parsed, dict):
                params = parsed
        except Exception:  # noqa: BLE001
            pass
    return DocumentRead(
        id=d.id,
        bail_id=d.bail_id,
        locataire_id=d.locataire_id,
        logement_id=getattr(d, "logement_id", None),
        type=d.type,
        titre=d.titre,
        params=params,
        created_at=d.created_at,
        envoye_le=d.envoye_le,
        envoye_a=d.envoye_a,
        ouvert_le=d.ouvert_le,
        signed_at=d.signed_at,
        signed_by_name=d.signed_by_name,
        source=(getattr(d, "source", "genere") or "genere"),
        filename=getattr(d, "filename", None),
        remplace_document_id=getattr(d, "remplace_document_id", None),
        signature_requise=d.type not in SIGNATURE_NON_REQUISE,
    )


async def save_document(
    db,
    *,
    bail_id: Optional[int],
    locataire_id: Optional[int],
    immeuble_id: Optional[int],
    doc_type: str,
    titre: str,
    params: Optional[dict],
    pdf: bytes,
    created_by_email: Optional[str],
) -> ImmDocument:
    """Enregistre un document généré (appelé par les endpoints de
    génération — extras TAL). Flush seulement : le commit appartient
    à l'appelant."""
    obj = ImmDocument(
        bail_id=bail_id,
        locataire_id=locataire_id,
        immeuble_id=immeuble_id,
        type=doc_type,
        titre=titre,
        params_json=(
            json.dumps(params, default=str) if params else None
        ),
        pdf_blob=pdf,
        created_by_email=created_by_email,
    )
    db.add(obj)
    await db.flush()
    return obj


@router.get("/baux/{bail_id}/documents", response_model=List[DocumentRead])
async def list_bail_documents(
    bail_id: int,
    db: DBSession,
    user: CurrentUser,
    categorie: str = "tout",
) -> List[DocumentRead]:
    """``categorie=dossier`` : seulement les pièces du dossier (signées
    ou importées) — les simples communications sont exclues."""
    _require_volet(user)
    rows = (
        await db.execute(
            select(ImmDocument)
            .where(ImmDocument.bail_id == bail_id)
            .order_by(ImmDocument.created_at.desc(), ImmDocument.id.desc())
        )
    ).scalars().all()
    if categorie == "dossier":
        rows = [d for d in rows if _est_dossier(d)]
    return [_doc_read(d) for d in rows]


@router.get(
    "/logements/{logement_id}/documents",
    response_model=List[DocumentRead],
)
async def list_logement_documents(
    logement_id: int,
    db: DBSession,
    user: CurrentUser,
    categorie: str = "tout",
) -> List[DocumentRead]:
    """Documents des baux (passés et actifs) du logement + ceux importés
    directement sur sa fiche. ``categorie=dossier`` exclut les simples
    communications."""
    _require_volet(user)
    from app.models.immobilier import Bail as _Bail

    bail_ids = [
        r[0]
        for r in (
            await db.execute(
                select(_Bail.id).where(_Bail.logement_id == logement_id)
            )
        ).all()
    ]
    cond = ImmDocument.logement_id == logement_id
    if bail_ids:
        cond = cond | ImmDocument.bail_id.in_(bail_ids)
    rows = (
        await db.execute(
            select(ImmDocument)
            .where(cond)
            .order_by(ImmDocument.created_at.desc(), ImmDocument.id.desc())
        )
    ).scalars().all()
    if categorie == "dossier":
        rows = [d for d in rows if _est_dossier(d)]
    return [_doc_read(d) for d in rows]


@router.get(
    "/locataires/{locataire_id}/documents",
    response_model=List[DocumentRead],
)
async def list_locataire_documents(
    locataire_id: int,
    db: DBSession,
    user: CurrentUser,
    categorie: str = "tout",
) -> List[DocumentRead]:
    """Documents du locataire : les siens (DPA…) + ceux de ses baux.
    ``categorie=dossier`` = uniquement les pièces signées ou importées —
    c'est ce qu'affiche la section Documents de sa fiche ; ses courriels
    sont dans Communications."""
    _require_volet(user)
    bail_ids = [
        r[0]
        for r in (
            await db.execute(
                select(Bail.id).where(Bail.locataire_id == locataire_id)
            )
        ).all()
    ]
    cond = ImmDocument.locataire_id == locataire_id
    if bail_ids:
        cond = cond | ImmDocument.bail_id.in_(bail_ids)
    rows = (
        await db.execute(
            select(ImmDocument)
            .where(cond)
            .order_by(ImmDocument.created_at.desc(), ImmDocument.id.desc())
        )
    ).scalars().all()
    if categorie == "dossier":
        rows = [d for d in rows if _est_dossier(d)]
    return [_doc_read(d) for d in rows]


@router.get("/documents/{doc_id}/pdf")
async def get_document_pdf(
    doc_id: int, db: DBSession, user: CurrentUser
):
    _require_volet(user)
    from fastapi.responses import Response

    d = (
        await db.execute(
            select(ImmDocument)
            .options(undefer(ImmDocument.pdf_blob))
            .where(ImmDocument.id == doc_id)
        )
    ).scalar_one_or_none()
    if d is None or not d.pdf_blob:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    fname = f"{d.type.replace('_', '-')}-{d.id}.pdf"
    return Response(
        content=d.pdf_blob,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{fname}"'
        },
    )


@router.delete(
    "/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_document(
    doc_id: int, db: DBSession, user: CurrentUser
) -> None:
    """Supprimer un document DÉ-POINTE aussi ce qui le référence (retour
    Phil 2026-07-27 : « ils sont reliés ») :
    - bail courant → le bouton « Bail » retombe sur le signé en ligne ;
    - avis de renouvellement → le cycle sans réponse est supprimé, la
      ligne redevient « à envoyer » dans Suivis annuels ;
    - relevé 31 → statut ramené à « à produire »."""
    _require_volet(user)
    d = await db.get(ImmDocument, doc_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    if d.signed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Un document signé ne peut pas être supprimé.",
        )
    from sqlalchemy import update as _update

    from app.models.immobilier import BailRenouvellement, Releve31

    # Garde-fou (audit 2026-07-31) : le PDF courant d'un bail ACTIF ne
    # se supprime pas — il se REMPLACE (l'activation venait de lui).
    if d.type == "bail":
        proprietaire = (
            await db.execute(
                select(Bail).where(
                    Bail.document_id == doc_id,
                    Bail.status == "actif",
                )
            )
        ).scalars().first()
        if proprietaire is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Ce PDF est le bail courant d'un bail ACTIF — "
                    "utilise « Remplacer » pour le corriger."
                ),
            )
    await db.execute(
        _update(Bail)
        .where(Bail.document_id == doc_id)
        .values(document_id=None)
    )
    renous = (
        await db.execute(
            select(BailRenouvellement).where(
                BailRenouvellement.document_id == doc_id
            )
        )
    ).scalars().all()
    for r in renous:
        if r.status == "propose":
            # Cycle encore PROPOSÉ (même chiffré) : supprimer l'avis
            # annule le cycle — la ligne redevient « à préparer »
            # (retour Phil 2026-07-30). Un cycle accepté/refusé/négocié
            # garde son historique, on ne fait que dé-pointer.
            await db.delete(r)
        else:
            r.document_id = None
    releves = (
        await db.execute(
            select(Releve31).where(Releve31.document_id == doc_id)
        )
    ).scalars().all()
    for rel in releves:
        rel.document_id = None
        if rel.statut in ("produit", "remis"):
            rel.statut = "a_produire"
    # Drive ↔ documents locatifs (v17b) : la copie Drive du document,
    # s'il y en a une, part à la CORBEILLE (réversible — jamais de
    # suppression permanente). Best-effort : Drive down ne bloque pas.
    if getattr(d, "drive_file_id", None):
        try:
            from app.services.drive_api import trash_file
            from app.services.drive_auto_upload_dispatcher import (
                resolve_drive_owner_user_id,
            )

            owner_id = await resolve_drive_owner_user_id(db)
            if owner_id is not None:
                await trash_file(owner_id, db, d.drive_file_id)
        except Exception:  # noqa: BLE001 — la suppression prime
            log.exception(
                "Corbeille Drive échouée pour le document %s", doc_id
            )
    await db.delete(d)
    await db.commit()


async def _resolve_destinataire(
    db, d: ImmDocument, email: Optional[str]
) -> tuple[Optional[Locataire], str]:
    """Destinataire d'un envoi : email explicite sinon le courriel du
    locataire (direct ou via le bail). 400 si aucun."""
    locataire: Optional[Locataire] = None
    if d.locataire_id:
        locataire = await db.get(Locataire, d.locataire_id)
    elif d.bail_id:
        bail = await db.get(Bail, d.bail_id)
        if bail:
            locataire = await db.get(Locataire, bail.locataire_id)
    dest = (email or "").strip() or (
        (locataire.email or "").strip() if locataire else ""
    )
    if not dest:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Aucun courriel destinataire — ajoute un courriel au "
                "locataire ou fournis-en un."
            ),
        )
    return locataire, dest


class EnvoyerSignatureRequest(BaseModel):
    email: Optional[EmailStr] = None  # défaut : courriel du locataire


class EnvoyerSignatureResult(BaseModel):
    document_id: int
    envoye_a: str
    envoye_le: datetime
    url: str


def _mail_html(titre: str, locataire_name: str, url: str) -> str:
    first = (locataire_name or "").strip().split(" ")[0] or "Bonjour"
    return f"""\
<div style="font-family:Helvetica,Arial,sans-serif;color:#111;line-height:1.5;max-width:640px">
  <p>Bonjour {first},</p>
  <p>Un document vous est transmis pour consultation et signature :
     <strong>{titre}</strong>.</p>
  <p style="margin:20px 0 6px 0">
    <a href="{url}" style="display:inline-block;background:#d89b3c;color:#111;
       padding:12px 20px;border-radius:8px;font-weight:bold;
       text-decoration:none">Consulter et signer le document</a>
  </p>
  <p style="margin:0 0 16px 0;font-size:12px;color:#555">Ou copiez ce lien : {url}</p>
  <p style="margin:24px 0 0 0;color:#555;font-size:12px">
    Horizon Services Immobiliers
  </p>
</div>
"""


@router.post(
    "/documents/{doc_id}/envoyer-signature",
    response_model=EnvoyerSignatureResult,
)
async def envoyer_signature(
    doc_id: int,
    payload: EnvoyerSignatureRequest,
    db: DBSession,
    user: CurrentUser,
) -> EnvoyerSignatureResult:
    """Envoie le document au locataire pour signature en ligne (lien
    public tokenisé). Envoi MANUEL uniquement — rien d'automatique."""
    _require_volet(user)
    d = await db.get(ImmDocument, doc_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    if d.signed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ce document est déjà signé.",
        )
    if d.type in SIGNATURE_NON_REQUISE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Ce document ne requiert pas de signature — utilise "
                "l'envoi par courriel."
            ),
        )

    locataire, dest = await _resolve_destinataire(db, d, payload.email)

    if not d.signature_token:
        d.signature_token = secrets.token_urlsafe(32)
        # COMMIT avant l'envoi, pas un simple flush : le lien part dans
        # la boîte du locataire et n'en ressortira jamais. Si la
        # transaction échoue APRÈS l'envoi, un flush laisserait le
        # locataire avec un lien dont le jeton n'existe nulle part —
        # « lien invalide » (bug du 2026-08-19 : deux consentements
        # reçus, aucun jeton en base).
        await db.commit()
        await db.refresh(d)
    url = f"{public_base()}/sign-document/{d.signature_token}"

    # Audit 2026-08-17 : porte UNIQUE des courriels au locataire
    # (profil d'expéditeur + journal d'audit + fil de la fiche).
    from app.services.locatif_mail import (
        EnvoiLocataireError,
        envoyer_au_locataire,
    )

    try:
        await envoyer_au_locataire(
            db,
            destinataires=[dest],
            sujet=f"{d.titre} — Horizon Services Immobiliers",
            corps_html=_mail_html(
                d.titre,
                locataire.full_name if locataire else "",
                url,
            ),
            type_envoi="document_signature",
            locataire_id=d.locataire_id,
            locataire_nom=(locataire.full_name if locataire else None),
            bail_id=d.bail_id,
            immeuble_id=d.immeuble_id,
            document_id=d.id,
            auteur_email=getattr(user, "email", None),
            resume_fiche=(
                f"Document envoyé pour signature : {d.titre} (à {dest})"
            ),
        )
    except EnvoiLocataireError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )
    except Exception as exc:  # noqa: BLE001 — réseau/Graph
        log.exception("Envoi document %s échoué", doc_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Envoi courriel échoué : {exc}",
        )

    d.envoye_le = _now()
    d.envoye_a = dest[:320]

    # (La trace — journal d'audit + fil de la fiche locataire — est
    # posée par envoyer_au_locataire : plus de doublon ici.)

    await db.commit()
    await db.refresh(d)
    return EnvoyerSignatureResult(
        document_id=d.id,
        envoye_a=dest,
        envoye_le=d.envoye_le,
        url=url,
    )


class EnvoyerCourrielResult(BaseModel):
    document_id: int
    envoye_a: str
    envoye_le: datetime


def _mail_html_piece_jointe(
    titre: str, locataire_name: str, doc_type: str, url: Optional[str]
) -> str:
    first = (locataire_name or "").strip().split(" ")[0] or "Bonjour"
    ligne_extra = ""
    if doc_type == "rappel_paiement":
        ligne_extra = (
            "<p><strong>Le paiement de votre loyer est exigé "
            "immédiatement.</strong></p>"
        )
    bloc_lien = (
        f"""
  <p style="margin:20px 0 6px 0">
    <a href="{url}" style="display:inline-block;background:#d89b3c;color:#111;
       padding:12px 20px;border-radius:8px;font-weight:bold;
       text-decoration:none">Consulter le document en ligne</a>
  </p>
  <p style="margin:0 0 16px 0;font-size:12px;color:#555">Ou copiez ce lien : {url}</p>
"""
        if url
        else ""
    )
    return f"""\
<div style="font-family:Helvetica,Arial,sans-serif;color:#111;line-height:1.5;max-width:640px">
  <p>Bonjour {first},</p>
  <p>Veuillez trouver ci-joint le document suivant :
     <strong>{titre}</strong>.</p>
  {ligne_extra}
  {bloc_lien}
  <p style="margin:24px 0 0 0;color:#555;font-size:12px">
    Horizon Services Immobiliers
  </p>
</div>
"""


@router.post(
    "/documents/{doc_id}/envoyer-courriel",
    response_model=EnvoyerCourrielResult,
)
async def envoyer_courriel(
    doc_id: int,
    payload: EnvoyerSignatureRequest,
    db: DBSession,
    user: CurrentUser,
) -> EnvoyerCourrielResult:
    """Envoie le document par SIMPLE COURRIEL avec le PDF en pièce
    jointe — pour les documents sans signature (avis de retard, avis
    d'accès…). Envoi MANUEL uniquement."""
    _require_volet(user)
    d = (
        await db.execute(
            select(ImmDocument)
            .options(undefer(ImmDocument.pdf_blob))
            .where(ImmDocument.id == doc_id)
        )
    ).scalar_one_or_none()
    if d is None or not d.pdf_blob:
        raise HTTPException(status_code=404, detail="Document introuvable.")

    locataire, dest = await _resolve_destinataire(db, d, payload.email)

    # Un relevé 31 ne part JAMAIS sans son numéro officiel (émis par
    # Revenu Québec à la production) — retour Phil 2026-07-31.
    if d.type == "releve31":
        from app.models.immobilier import Releve31

        rel = (
            await db.execute(
                select(Releve31).where(Releve31.document_id == d.id)
            )
        ).scalars().first()
        if rel is not None and not (rel.numero_releve or "").strip():
            raise HTTPException(
                status_code=422,
                detail=(
                    "Colle d'abord le numéro du relevé (émis par "
                    "Revenu Québec) avant de l'envoyer au locataire."
                ),
            )

    # Lien de consultation tokenisé (page publique SANS bloc signature
    # pour ces types) → l'ouverture est horodatée comme pour une
    # signature : suivi d'ouverture universel (retour Phil 2026-07-20).
    if not d.signature_token:
        d.signature_token = secrets.token_urlsafe(32)
        # COMMIT avant l'envoi, pas un simple flush : le lien part dans
        # la boîte du locataire et n'en ressortira jamais. Si la
        # transaction échoue APRÈS l'envoi, un flush laisserait le
        # locataire avec un lien dont le jeton n'existe nulle part —
        # « lien invalide » (bug du 2026-08-19 : deux consentements
        # reçus, aucun jeton en base).
        await db.commit()
        await db.refresh(d)
    url = f"{public_base()}/sign-document/{d.signature_token}"

    from app.services.locatif_mail import (
        EnvoiLocataireError,
        envoyer_au_locataire,
    )

    try:
        await envoyer_au_locataire(
            db,
            destinataires=[dest],
            sujet=f"{d.titre} — Horizon Services Immobiliers",
            corps_html=_mail_html_piece_jointe(
                d.titre,
                locataire.full_name if locataire else "",
                d.type,
                url,
            ),
            type_envoi="document_courriel",
            locataire_id=d.locataire_id,
            locataire_nom=(locataire.full_name if locataire else None),
            bail_id=d.bail_id,
            immeuble_id=d.immeuble_id,
            document_id=d.id,
            auteur_email=getattr(user, "email", None),
            resume_fiche=f"Document envoyé par courriel : {d.titre} (à {dest})",
            attachments=[
                EmailAttachment(
                    name=f"{d.type.replace('_', '-')}.pdf",
                    content_bytes=d.pdf_blob,
                    content_type="application/pdf",
                )
            ],
        )
    except Exception as exc:  # noqa: BLE001 — réseau/Graph
        log.exception("Envoi courriel document %s échoué", doc_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Envoi courriel échoué : {exc}",
        )

    d.envoye_le = _now()
    d.envoye_a = dest[:320]

    # (Trace posée par envoyer_au_locataire — pas de doublon ici.)

    await db.commit()
    await db.refresh(d)
    return EnvoyerCourrielResult(
        document_id=d.id, envoye_a=dest, envoye_le=d.envoye_le
    )


# ── IMPORT de documents + « document courant » ────────────────────────
# Retour Phil 2026-07-27 : démêlage communications / documents.
#   • Documents = ce qui est SIGNÉ ou DÉPOSÉ au dossier (≠ courriels).
#   • Le bail, l'avis de renouvellement et le relevé 31 ont chacun UN
#     document courant : c'est lui qui s'ouvre au clic depuis la page.
#     En importer un nouveau change le pointeur ; l'ancien reste en base
#     et continue d'apparaître dans les Documents (historique).

_MAX_UPLOAD = 20_000_000  # 20 Mo
_ALLOWED_UPLOAD = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


async def _read_upload(file: UploadFile) -> tuple[bytes, str]:
    """Lit et valide un fichier téléversé. Retourne (contenu, nom)."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Fichier vide.")
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(
            status_code=400, detail="Fichier trop volumineux (max 20 Mo)."
        )
    ctype = (file.content_type or "").split(";")[0].strip().lower()
    is_pdf = data[:5].startswith(b"%PDF-")
    if not is_pdf and ctype not in _ALLOWED_UPLOAD:
        raise HTTPException(
            status_code=400,
            detail="Format accepté : PDF, JPG ou PNG.",
        )
    return data, (file.filename or "document")[:255]


async def _import_document(
    db,
    user,
    *,
    file: UploadFile,
    doc_type: str,
    titre: Optional[str],
    bail_id: Optional[int] = None,
    locataire_id: Optional[int] = None,
    logement_id: Optional[int] = None,
    immeuble_id: Optional[int] = None,
    remplace_document_id: Optional[int] = None,
) -> ImmDocument:
    data, fname = await _read_upload(file)
    obj = ImmDocument(
        bail_id=bail_id,
        locataire_id=locataire_id,
        logement_id=logement_id,
        immeuble_id=immeuble_id,
        type=doc_type,
        titre=(titre or "").strip() or fname,
        source="importe",
        filename=fname,
        remplace_document_id=remplace_document_id,
        pdf_blob=data,
        created_by_email=getattr(user, "email", None),
    )
    db.add(obj)
    await db.flush()
    return obj


@router.post("/documents/import", response_model=DocumentRead)
async def import_document(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
    type: str = Form("autre"),
    titre: Optional[str] = Form(None),
    bail_id: Optional[int] = Form(None),
    locataire_id: Optional[int] = Form(None),
    logement_id: Optional[int] = Form(None),
    immeuble_id: Optional[int] = Form(None),
) -> DocumentRead:
    """Dépose un document au dossier (bouton « Importer » des sections
    Documents). Aucun envoi, aucune signature : c'est une pièce classée.

    ``type`` : idéalement une clé de ``IMM_DOC_TYPES`` (menus du front) ;
    une valeur hors liste est CONSERVÉE telle quelle (rétro-compat).
    ⚠️ Un PDF de type « bail » rattaché à un BAIL doit passer par
    ``POST /baux/{id}/document`` (c'est lui qui pose ``bail.document_id``
    et active le bail) — ici, un « bail » n'est qu'une pièce au dossier
    du locataire, à joindre plus tard via « Joindre le bail signé »."""
    _require_volet(user)
    if not any([bail_id, locataire_id, logement_id, immeuble_id]):
        raise HTTPException(
            status_code=422,
            detail="Précise à quoi rattacher le document.",
        )
    obj = await _import_document(
        db, user,
        file=file,
        doc_type=(type or "autre").strip()[:48] or "autre",
        titre=titre,
        bail_id=bail_id,
        locataire_id=locataire_id,
        logement_id=logement_id,
        immeuble_id=immeuble_id,
    )
    await db.commit()
    await db.refresh(obj)
    log.info("Document importé #%s (%s) par %s", obj.id, obj.type, user.email)
    return _doc_read(obj)


@router.post("/baux/{bail_id}/document", response_model=DocumentRead)
async def upload_bail_document(
    bail_id: int,
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
    date_entree: Optional[str] = Form(None),
    au_mois: Optional[str] = Form(None),
) -> DocumentRead:
    """Importe LE bail signé — ou le remplace. Le nouveau devient celui
    qui s'ouvre au clic ; l'ancien reste dans les Documents du logement
    et du locataire. ``date_entree`` (AAAA-MM-JJ, demandée à l'import) =
    entrée en vigueur affichée dans le titre « Bail signé 2026-07-01 » —
    défaut : la date de début du bail (retour Phil 2026-07-27).
    ``au_mois`` ('true'/'false', demandé au même moment) marque le bail
    au mois — chambres, reconduction auto (retour Phil 2026-07-28)."""
    _require_volet(user)
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    if au_mois is not None and au_mois.strip() != "":
        bail.au_mois = au_mois.strip().lower() in ("true", "1", "oui")
    ancien = bail.document_id
    vigueur = (date_entree or "").strip() or (
        bail.date_debut.isoformat() if bail.date_debut else ""
    )
    obj = await _import_document(
        db, user,
        file=file,
        doc_type="bail",
        titre=f"Bail signé {vigueur}".strip(),
        bail_id=bail.id,
        locataire_id=bail.locataire_id,
        logement_id=bail.logement_id,
        remplace_document_id=ancien,
    )
    bail.document_id = obj.id
    # Bail signé importé : un bail « proposé » devient ACTIF, et le
    # dossier de relocation lié passe à « Reloué » — partout, le
    # logement est considéré loué par ce locataire (retour Phil
    # 2026-07-31).
    if bail.status == "propose":
        # Garde-fou C4 (2026-08-13) : un bail ACTIF déjà ÉCHU sur ce
        # logement (sa fin précède le début du nouveau) est terminé
        # automatiquement — sinon les deux coexistent et le suivi des
        # loyers double la ligne.
        from app.services.locatif_depart import terminer_baux_echus_avant

        await terminer_baux_echus_avant(
            db, bail.logement_id, bail.date_debut, exclure_bail_id=bail.id
        )
        # Jamais deux baux ACTIFS qui se chevauchent sur le même
        # logement (audit 2026-07-31).
        chevauche = (
            await db.execute(
                select(Bail).where(
                    Bail.logement_id == bail.logement_id,
                    Bail.id != bail.id,
                    Bail.status == "actif",
                    Bail.date_debut <= bail.date_fin,
                    Bail.date_fin >= bail.date_debut,
                )
            )
        ).scalars().first()
        if chevauche is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Un bail ACTIF chevauche ces dates sur ce logement "
                    f"(fin le {chevauche.date_fin}) — termine-le "
                    "(résiliation) ou corrige les dates du nouveau "
                    "bail avant d'importer."
                ),
            )
        bail.status = "actif"
    try:
        from app.models.immobilier import (
            LocationDossier,
            LocationDossierStatut,
            Logement,
        )

        # Miroir « loyer demandé » (2026-08-13) : bail ACTIF au dossier
        # → le logement occupé suit le loyer réel du bail.
        if bail.status == "actif" and bail.loyer_mensuel is not None:
            lg_sync = await db.get(Logement, bail.logement_id)
            if lg_sync is not None:
                lg_sync.loyer_demande = bail.loyer_mensuel

        from app.services.locatif_depart import (
            DOSSIER_STATUTS_REGLES,
            recaler_statut_logement,
        )

        dossier_reloc = (
            (
                await db.execute(
                    select(LocationDossier).where(
                        LocationDossier.nouveau_bail_id == bail.id,
                        LocationDossier.statut.notin_(
                            list(DOSSIER_STATUTS_REGLES)
                        ),
                    )
                )
            )
            .scalars()
            .first()
        )
        if dossier_reloc is not None:
            dossier_reloc.statut = LocationDossierStatut.RELOUE.value
            if dossier_reloc.reloue_le is None:
                dossier_reloc.reloue_le = _now().date()
        # Règle UNIQUE du statut du logement (M2/M3, audit 2026-08-13) :
        # le service recalcule occupé/réservé/vacant d'après les baux.
        if bail.logement_id:
            await recaler_statut_logement(db, bail.logement_id)
    except Exception:  # noqa: BLE001 — best-effort
        log.exception(
            "Transition relocation après import du bail %s", bail_id
        )
    await db.commit()
    await db.refresh(obj)
    log.info(
        "Bail %s : document courant → #%s (ancien #%s)",
        bail_id, obj.id, ancien,
    )
    return _doc_read(obj)


@router.post(
    "/renouvellements/{renouvellement_id}/document",
    response_model=DocumentRead,
)
async def upload_renouvellement_document(
    renouvellement_id: int,
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
    nouveau_loyer: Optional[str] = Form(None),
    nouvelle_date_debut: Optional[str] = Form(None),
    nouvelle_date_fin: Optional[str] = Form(None),
    deja_accepte: Optional[str] = Form(None),
) -> DocumentRead:
    """Importe (ou remplace) l'avis de renouvellement courant. L'ancien
    reste au dossier."""
    _require_volet(user)
    from app.models.immobilier import BailRenouvellement

    r = await db.get(BailRenouvellement, renouvellement_id)
    if r is None:
        raise HTTPException(
            status_code=404, detail="Renouvellement introuvable."
        )
    bail = await db.get(Bail, r.bail_id)
    ancien = r.document_id
    obj = await _import_document(
        db, user,
        file=file,
        doc_type="avis_renouvellement",
        titre=f"Avis de renouvellement — {r.avis_envoye_le}",
        bail_id=r.bail_id,
        locataire_id=bail.locataire_id if bail else None,
        logement_id=bail.logement_id if bail else None,
        remplace_document_id=ancien,
    )
    r.document_id = obj.id
    _appliquer_infos_avis(
        r, nouveau_loyer, nouvelle_date_debut, nouvelle_date_fin,
        deja_accepte,
    )
    await db.commit()
    await db.refresh(obj)
    return _doc_read(obj)


def _appliquer_infos_avis(
    r, nouveau_loyer, nouvelle_date_debut, nouvelle_date_fin,
    deja_accepte=None,
) -> None:
    """Reporte sur le cycle les infos de l'avis IMPORTÉ (retour Phil
    2026-07-31 : « je devrais pouvoir écrire les infos qui sont sur
    l'avis que j'importe ») — champs vides ignorés."""
    from datetime import date as _d

    if nouveau_loyer and str(nouveau_loyer).strip():
        try:
            r.nouveau_loyer = float(str(nouveau_loyer).strip())
        except ValueError:
            pass
    for champ, brut in (
        ("nouvelle_date_debut", nouvelle_date_debut),
        ("nouvelle_date_fin", nouvelle_date_fin),
    ):
        if brut and str(brut).strip():
            try:
                setattr(r, champ, _d.fromisoformat(str(brut).strip()))
            except ValueError:
                pass
    # Un avis importé DÉJÀ SIGNÉ par le locataire (papier) arrive vert
    # « Accepté » — pas « en attente » (retour Phil 2026-07-31).
    if deja_accepte and str(deja_accepte).strip().lower() in (
        "true", "1", "oui",
    ):
        r.status = "accepte"
        r.reponse_le = _d.today()


@router.post("/renouvellements/importer", response_model=DocumentRead)
async def importer_avis_pour_bail(
    db: DBSession,
    user: CurrentUser,
    bail_id: int = Form(...),
    file: UploadFile = File(...),
    nouveau_loyer: Optional[str] = Form(None),
    nouvelle_date_debut: Optional[str] = Form(None),
    nouvelle_date_fin: Optional[str] = Form(None),
    deja_accepte: Optional[str] = Form(None),
) -> DocumentRead:
    """Importe un avis de renouvellement pour un bail SANS cycle en cours
    (ex. avis papier envoyé avant Kratos) : crée le renouvellement
    (avis envoyé aujourd'hui, statut proposé) et y attache le PDF comme
    avis courant. La prochaine préparation d'avis démarrera un nouveau
    cycle qui prendra le dessus (retour Phil 2026-07-27)."""
    _require_volet(user)
    from datetime import date as _date

    from app.models.immobilier import BailRenouvellement

    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    r = BailRenouvellement(
        bail_id=bail.id,
        avis_envoye_le=_date.today(),
    )
    r.created_at = _now()
    r.updated_at = _now()
    db.add(r)
    await db.flush()
    obj = await _import_document(
        db, user,
        file=file,
        doc_type="avis_renouvellement",
        titre=f"Avis de renouvellement — {r.avis_envoye_le}",
        bail_id=bail.id,
        locataire_id=bail.locataire_id,
        logement_id=bail.logement_id,
    )
    r.document_id = obj.id
    _appliquer_infos_avis(
        r, nouveau_loyer, nouvelle_date_debut, nouvelle_date_fin,
        deja_accepte,
    )
    await db.commit()
    await db.refresh(obj)
    log.info(
        "Avis de renouvellement importé pour bail %s (renouvellement #%s)",
        bail_id, r.id,
    )
    return _doc_read(obj)
