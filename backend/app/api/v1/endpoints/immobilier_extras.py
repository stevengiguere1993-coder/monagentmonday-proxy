"""Extensions immobilier — formulaires TAL, renouvellements, vue par
entreprise propriétaire."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.orm import undefer

from app.api.deps import CurrentUser, DBSession
from app.models.entreprise import Entreprise
from app.models.immobilier import (
    Bail,
    BailRenouvellement,
    BailStatus,
    LocationDossier,
    LocationDossierStatut,
    Evaluation,
    EvaluationKind,
    Hypotheque,
    HypothequeStatus,
    ImmDocTemplate,
    ImmDocument,
    Immeuble,
    ImmeubleOwnership,
    Logement,
    LogementStatus,
    Locataire,
)
from app.schemas.immobilier_extras import (
    EntrepriseImmobilierImmeubleItem,
    EntrepriseImmobilierSummary,
    EnvoyerRenouvellementRequest,
    EnvoyerRenouvellementResult,
    RenouvellementOverview,
    TalFormRequest,
    TalFormType,
)
from app.services.automation_state import (
    get_automation_config,
    set_automation_config,
)
from app.services.bail_renouvellement import (
    arrondir_loyer,
    fin_reconduite,
    send_renouvellement_for_bail,
)
from app.services.tal_forms import (
    GABARIT_VARIABLES,
    GABARITS_DEFAUT,
    SIGNATURE_NON_REQUISE,
    TalContext,
    available_form_types,
    generate_tal_pdf,
)
from app.services.tal_officiel import is_official, validate_template


log = logging.getLogger(__name__)
router = APIRouter(prefix="/immobilier", tags=["immobilier"])


def _require_volet(user: CurrentUser) -> None:
    volets = getattr(user, "volets", None)
    if volets is None or "immobilier" not in volets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Volet « Gestion immobilière » non autorisé.",
        )


# ─── Catalogue des formulaires TAL ─────────────────────────────────────


_TAL_LABELS = {
    "avis_modification": (
        "Avis d'augmentation / modification du bail",
        "Formulaire officiel TAL-806 — hausse de loyer et modification "
        "d'une autre condition du bail (art. 1942-1943 C.c.Q., réponse "
        "du locataire en 1 mois).",
    ),
    "avis_non_reconduction": (
        "Avis de non-reconduction (par le locataire)",
        "Formulaire officiel TAL-807 — le locataire avise qu'il quitte à "
        "la fin du bail (art. 1946 C.c.Q.) ; c'est lui qui le signe.",
    ),
    "avis_reprise": (
        "Avis de reprise de logement",
        "Formulaire officiel TAL-809 — reprise pour s'y loger ou y loger "
        "un proche (art. 1960 C.c.Q., 6 mois avant la fin du bail, "
        "réponse du locataire en 1 mois).",
    ),
    "avis_travaux_majeurs": (
        "Avis de réparation ou d'amélioration majeure",
        "Formulaire officiel TAL-808 — travaux majeurs non urgents "
        "(art. 1922-1923 C.c.Q., 10 jours d'avis, 3 mois si évacuation "
        "de plus de 7 jours).",
    ),
    "reponse_cession": (
        "Réponse à un avis de cession de bail",
        "Formulaire officiel TAL-828 (avis reçus depuis le 21 février "
        "2024) — accepter ou refuser dans les 15 jours (art. 1871 et "
        "1978.2 C.c.Q.).",
    ),
    "rappel_paiement": (
        "Avis de retard de paiement",
        "Lettre exigeant le paiement IMMÉDIAT du loyer impayé — envoi "
        "par courriel, sans signature.",
    ),
    "avis_acces": (
        "Avis d'accès au logement",
        "Visite ou travaux mineurs avec préavis de 24 h (art. 1931-1933 "
        "C.c.Q.) — envoi par courriel, sans signature.",
    ),
    "demande_assurance": (
        "Demande de preuve d'assurance (courriel)",
        "Courriel annuel demandant l'attestation d'assurance habitation "
        "du locataire — s'envoie depuis Suivis annuels → Assurances. Le "
        "titre du gabarit = l'objet du courriel.",
    ),
    "consentement_communications": (
        "Consentement communications électroniques",
        "Consentement du locataire à recevoir avis et documents par "
        "courriel (RLRQ, c. C-1.1). PRÉPARÉ automatiquement au dossier "
        "à la création du bail — jamais envoyé tout seul : c'est le "
        "bouton « Envoyer pour signature » de la fiche qui décide.",
    ),
}


async def _template_override(
    db, form_type: str, *, with_blob: bool = False
) -> Optional[ImmDocTemplate]:
    """PDF modèle remplacé par l'utilisateur pour ce formulaire, s'il y
    en a un (Paramètres → Modèles de documents)."""
    q = select(ImmDocTemplate).where(ImmDocTemplate.type == form_type)
    if with_blob:
        q = q.options(undefer(ImmDocTemplate.pdf_blob))
    return (await db.execute(q)).scalar_one_or_none()


@router.get("/tal/forms", response_model=List[TalFormType])
async def list_tal_forms(db: DBSession, user: CurrentUser) -> List[TalFormType]:
    _require_volet(user)
    overrides = {
        t.type: t
        for t in (await db.execute(select(ImmDocTemplate))).scalars().all()
    }
    out: List[TalFormType] = []
    for code in available_form_types():
        label, desc = _TAL_LABELS.get(code, (code.replace("_", " ").title(), ""))
        ov = overrides.get(code)
        out.append(
            TalFormType(
                code=code,
                label=label,
                description=desc,
                officiel=is_official(code),
                signature_requise=code not in SIGNATURE_NON_REQUISE,
                texte_modifiable=code in GABARITS_DEFAUT,
                custom_filename=ov.filename if ov else None,
                custom_uploaded_at=ov.updated_at if ov else None,
            )
        )
    return out


# ── Gabarits éditables des lettres maison (retard, accès) ─────────────
# Le texte vit dans automation_settings (clé immo.gabarit.<type>) —
# retour Phil 2026-07-20 : « ceux qui ne sont pas du TAL, il faut
# pouvoir les modifier ».


class GabaritRead(BaseModel):
    code: str
    titre: str
    paragraphes: List[str]
    variables: List[str]
    #: True si un texte personnalisé est enregistré (≠ défaut).
    personnalise: bool = False


class GabaritUpdate(BaseModel):
    titre: Optional[str] = Field(default=None, max_length=120)
    #: None ou liste vide = revenir au texte d'origine.
    paragraphes: Optional[List[str]] = None


def _gabarit_key(form_type: str) -> str:
    return f"immo.gabarit.{form_type}"


@router.get("/tal/gabarits/{form_type}", response_model=GabaritRead)
async def get_tal_gabarit(
    form_type: str, user: CurrentUser
) -> GabaritRead:
    _require_volet(user)
    defaut = GABARITS_DEFAUT.get(form_type)
    if defaut is None:
        raise HTTPException(
            status_code=404,
            detail="Cette lettre n'a pas de gabarit modifiable.",
        )
    cfg = await get_automation_config(_gabarit_key(form_type))
    paragraphes = cfg.get("paragraphes") if isinstance(cfg, dict) else None
    personnalise = bool(paragraphes)
    return GabaritRead(
        code=form_type,
        titre=(cfg.get("titre") if personnalise else None)
        or defaut["titre"],
        paragraphes=list(paragraphes or defaut["paragraphes"]),
        variables=GABARIT_VARIABLES.get(form_type, []),
        personnalise=personnalise,
    )


@router.put("/tal/gabarits/{form_type}", response_model=GabaritRead)
async def put_tal_gabarit(
    form_type: str,
    payload: GabaritUpdate,
    db: DBSession,
    user: CurrentUser,
) -> GabaritRead:
    _require_volet(user)
    defaut = GABARITS_DEFAUT.get(form_type)
    if defaut is None:
        raise HTTPException(
            status_code=404,
            detail="Cette lettre n'a pas de gabarit modifiable.",
        )
    paragraphes = [
        p.strip() for p in (payload.paragraphes or []) if p and p.strip()
    ]
    if paragraphes and sum(len(p) for p in paragraphes) > 20_000:
        raise HTTPException(status_code=422, detail="Gabarit trop long.")
    config: dict = {}
    if paragraphes:
        config = {"paragraphes": paragraphes}
        if (payload.titre or "").strip():
            config["titre"] = payload.titre.strip()
    # Liste vide/None = réinitialisation au texte d'origine.
    await set_automation_config(
        db, _gabarit_key(form_type), config,
        user_id=getattr(user, "id", None),
    )
    await db.commit()
    return GabaritRead(
        code=form_type,
        titre=config.get("titre") or defaut["titre"],
        paragraphes=paragraphes or list(defaut["paragraphes"]),
        variables=GABARIT_VARIABLES.get(form_type, []),
        personnalise=bool(paragraphes),
    )


async def _gabarit_override(form_type: str) -> Optional[dict]:
    """Override de texte pour une lettre, ou None (fail-safe)."""
    if form_type not in GABARITS_DEFAUT:
        return None
    cfg = await get_automation_config(_gabarit_key(form_type))
    if isinstance(cfg, dict) and cfg.get("paragraphes"):
        return cfg
    return None


@router.post("/tal/modeles/{form_type}/pdf", response_model=TalFormType)
async def upload_tal_template(
    form_type: str,
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
) -> TalFormType:
    """Remplace le PDF modèle d'un formulaire officiel (nouvelle version
    publiée par le TAL). Les noms de champs requis sont validés — un PDF
    incompatible est refusé plutôt que de générer des avis à blanc."""
    _require_volet(user)
    if not is_official(form_type):
        raise HTTPException(
            status_code=400,
            detail="Seuls les formulaires officiels TAL sont remplaçables.",
        )
    data = await file.read()
    if not data or not data[:5].startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="Le fichier doit être un PDF.")
    if len(data) > 15_000_000:
        raise HTTPException(status_code=400, detail="PDF trop volumineux (max 15 Mo).")
    try:
        missing = validate_template(form_type, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                "PDF incompatible — champs manquants : "
                + ", ".join(missing[:8])
                + ("…" if len(missing) > 8 else "")
            ),
        )
    existing = await _template_override(db, form_type)
    if existing is None:
        existing = ImmDocTemplate(type=form_type, pdf_blob=data)
        db.add(existing)
    else:
        existing.pdf_blob = data
    existing.filename = (file.filename or "modele.pdf")[:255]
    existing.uploaded_by_email = getattr(user, "email", None)
    await db.commit()
    await db.refresh(existing)
    label, desc = _TAL_LABELS.get(form_type, (form_type, ""))
    return TalFormType(
        code=form_type,
        label=label,
        description=desc,
        officiel=True,
        signature_requise=form_type not in SIGNATURE_NON_REQUISE,
        custom_filename=existing.filename,
        custom_uploaded_at=existing.updated_at,
    )


@router.delete(
    "/tal/modeles/{form_type}/pdf", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_tal_template(
    form_type: str, db: DBSession, user: CurrentUser
) -> None:
    """Revient au PDF officiel embarqué d'origine."""
    _require_volet(user)
    existing = await _template_override(db, form_type)
    if existing is not None:
        await db.delete(existing)
        await db.commit()


@router.get("/tal/apercu/{form_type}.pdf")
async def apercu_tal_pdf(
    form_type: str, db: DBSession, user: CurrentUser
) -> Response:
    """Aperçu d'un MODÈLE avec des données d'exemple — page Paramètres →
    Modèles de documents (retour Phil 2026-07-17 : « ils sont où les
    modèles ? »). La vraie génération se fait depuis un bail (préremplie)."""
    _require_volet(user)
    from datetime import date as _date, timedelta as _td

    demo_debut = _date.today().replace(day=1)
    if form_type not in available_form_types():
        raise HTTPException(
            status_code=404, detail="Modèle inconnu."
        )
    ctx = TalContext(
        locateur_nom="Horizon Services Immobiliers (exemple)",
        locateur_adresse="500 rue du Locateur, Montréal",
        locateur_telephone="514 555-0100",
        locateur_courriel="info@immohorizon.com",
        locataire_nom="Jean Tremblay (exemple)",
        locataire_email="jean@example.com",
        logement_adresse="123 rue Exemple",
        logement_numero="App. 4",
        logement_ville="Montréal",
        bail_date_debut=demo_debut,
        bail_date_fin=demo_debut + _td(days=364),
        bail_loyer_mensuel=1250.0,
        bail_chauffage_inclus=True,
        modif_mode="nouveau_loyer",
        nouveau_loyer=1300.0,
        nouvelle_date_debut=demo_debut + _td(days=365),
        nouvelle_date_fin=demo_debut + _td(days=729),
        montant_du=1250.0,
        mois_concerne=demo_debut,
        depart_date=demo_debut + _td(days=364),
        reprise_date=demo_debut + _td(days=365),
        reprise_beneficiaire="Philippe Meuser (exemple)",
        reprise_lien="moi-même",
        travaux_description="Réfection complète de la salle de bain (exemple)",
        travaux_date_debut=demo_debut + _td(days=30),
        travaux_duree_valeur="2",
        travaux_duree_unite="semaines",
        travaux_evacuation=True,
        travaux_evacuation_du=demo_debut + _td(days=30),
        travaux_evacuation_au=demo_debut + _td(days=35),
        travaux_indemnite=500.0,
        acces_date=demo_debut + _td(days=7),
        acces_plage="entre 9 h et 12 h",
        acces_motif="vérification de l'état du logement (exemple)",
        cession_decision="accepte",
        cession_date=demo_debut + _td(days=60),
    )
    ov = await _template_override(db, form_type, with_blob=True)
    pdf = generate_tal_pdf(
        form_type,
        ctx,
        template_bytes=ov.pdf_blob if ov else None,
        gabarit=await _gabarit_override(form_type),
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'inline; filename="apercu-{form_type}.pdf"'
            )
        },
    )


# ─── PDF generation pour un bail donné ─────────────────────────────────


async def _build_ctx_from_bail(
    db, bail: Bail, params: TalFormRequest
) -> TalContext:
    logement = await db.get(Logement, bail.logement_id)
    immeuble = await db.get(Immeuble, logement.immeuble_id) if logement else None
    locataire = await db.get(Locataire, bail.locataire_id)

    # Premier propriétaire enregistré comme locateur affiché
    locateur_nom = None
    if immeuble is not None:
        ownership = (
            await db.execute(
                select(ImmeubleOwnership).where(
                    ImmeubleOwnership.immeuble_id == immeuble.id
                )
            )
        ).scalars().first()
        if ownership is not None:
            ent = await db.get(Entreprise, ownership.entreprise_id)
            if ent is not None:
                locateur_nom = ent.name

    # Fin de renouvellement par défaut : art. 1941 C.c.Q. — un bail de
    # 12 mois se reconduit 12 mois (même jour un an plus tard, que le
    # bail soit du 1er juillet ou non) ; plus court = même durée.
    # (L'ancien calcul en jours décalait la date sur le PDF.)
    nouvelle_fin = params.nouvelle_date_fin
    if nouvelle_fin is None and bail.date_fin is not None:
        nouvelle_fin = fin_reconduite(bail.date_debut, bail.date_fin)

    # Avis de modification : le PDF coche TOUJOURS la 1re case (« votre
    # loyer actuel de X $ sera augmenté à Y $ ») — une hausse en % ou
    # en $ est convertie en NOUVEAU LOYER, arrondi au dollar supérieur
    # (retour Phil 2026-07-30).
    nouveau_loyer_final = params.nouveau_loyer
    courant_ = float(bail.loyer_mensuel or 0)
    if nouveau_loyer_final is None and params.hausse_pct is not None:
        nouveau_loyer_final = courant_ * (1 + params.hausse_pct / 100.0)
    elif nouveau_loyer_final is None and params.hausse_montant is not None:
        nouveau_loyer_final = courant_ + params.hausse_montant
    if nouveau_loyer_final is not None:
        nouveau_loyer_final = arrondir_loyer(nouveau_loyer_final)

    return TalContext(
        locateur_nom=locateur_nom,
        # Adresse du bureau (mandataire Horizon) — même adresse pour
        # toutes les compagnies de Phil.
        locateur_adresse="158 rue Maurice, Saint-Rémi (Québec) J0L 2L0",
        locataire_nom=locataire.full_name if locataire else None,
        locataire_email=locataire.email if locataire else None,
        logement_adresse=immeuble.address if immeuble else None,
        logement_numero=logement.numero if logement else None,
        logement_ville=immeuble.city if immeuble else None,
        bail_date_debut=bail.date_debut,
        bail_date_fin=bail.date_fin,
        bail_loyer_mensuel=float(bail.loyer_mensuel) if bail.loyer_mensuel else None,
        bail_chauffage_inclus=bool(bail.chauffage_inclus),
        bail_eau_chaude_inclus=bool(bail.eau_chaude_inclus),
        bail_electricite_inclus=bool(bail.electricite_inclus),
        bail_internet_inclus=bool(bail.internet_inclus),
        depot_garantie=(
            float(bail.depot_garantie)
            if bail.depot_garantie is not None
            else None
        ),
        modif_mode=(
            "nouveau_loyer"
            if nouveau_loyer_final is not None
            else params.modif_mode
        ),
        nouveau_loyer=nouveau_loyer_final,
        hausse_montant=None,
        hausse_pct=None,
        nouvelle_date_debut=params.nouvelle_date_debut
        or (bail.date_fin + timedelta(days=1) if bail.date_fin else None),
        nouvelle_date_fin=nouvelle_fin,
        motif_modification=params.motif,
        montant_du=params.montant_du,
        mois_concerne=params.mois_concerne,
        depart_date=params.depart_date or bail.date_fin,
        reprise_date=params.reprise_date,
        reprise_beneficiaire=params.reprise_beneficiaire,
        reprise_lien=params.reprise_lien,
        travaux_description=params.travaux_description,
        travaux_date_debut=params.travaux_date_debut,
        travaux_duree_valeur=params.travaux_duree_valeur,
        travaux_duree_unite=params.travaux_duree_unite,
        travaux_evacuation=params.travaux_evacuation,
        travaux_evacuation_du=params.travaux_evacuation_du,
        travaux_evacuation_au=params.travaux_evacuation_au,
        travaux_indemnite=params.travaux_indemnite,
        travaux_conditions=params.travaux_conditions,
        acces_date=params.acces_date,
        acces_plage=params.acces_plage,
        acces_motif=params.acces_motif,
        cession_decision=params.cession_decision,
        cession_date=params.cession_date,
        cession_accepte=params.cession_accepte,
        cession_motif_refus=params.cession_motif_refus,
    )


@router.post("/baux/{bail_id}/tal/{form_type}.pdf")
async def generate_bail_tal_pdf(
    bail_id: int,
    form_type: str,
    payload: TalFormRequest,
    db: DBSession,
    user: CurrentUser,
) -> Response:
    _require_volet(user)
    if form_type not in available_form_types():
        raise HTTPException(status_code=400, detail="Type de formulaire inconnu.")
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")

    ctx = await _build_ctx_from_bail(db, bail, payload)
    # Overrides de la fiche de préparation (retour Phil 2026-07-31) :
    # nom, adresse complète et loyer actuel modifiables.
    if payload.locataire_nom:
        ctx.locataire_nom = payload.locataire_nom
    if payload.logement_adresse:
        ctx.logement_adresse = payload.logement_adresse
        ctx.logement_numero = None
        ctx.logement_ville = None
    if payload.loyer_actuel is not None:
        ctx.bail_loyer_mensuel = float(payload.loyer_actuel)
    ov = await _template_override(db, form_type, with_blob=True)
    pdf_bytes = generate_tal_pdf(
        form_type,
        ctx,
        template_bytes=ov.pdf_blob if ov else None,
        gabarit=await _gabarit_override(form_type),
    )

    # CONSERVE le document (retour Phil 2026-07-17 : « ces documents-là,
    # ils sont où ? ») — visible/modifiable/envoyable depuis la fiche.
    try:
        from app.api.v1.endpoints.immobilier_documents import save_document

        logement = await db.get(Logement, bail.logement_id)
        label, _desc = _TAL_LABELS.get(
            form_type, (form_type.replace("_", " ").title(), "")
        )
        await save_document(
            db,
            bail_id=bail.id,
            locataire_id=bail.locataire_id,
            immeuble_id=logement.immeuble_id if logement else None,
            doc_type=form_type,
            titre=label,
            params=payload.model_dump(exclude_none=True, mode="json"),
            pdf=pdf_bytes,
            created_by_email=getattr(user, "email", None),
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — la génération prime sur l'archivage
        log.exception("Sauvegarde du document TAL échouée (bail %s)", bail_id)

    filename = f"{form_type.replace('_', '-')}-bail-{bail_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


async def preparer_consentement_communications(db, bail_id: int, user) -> bool:
    """Consentement aux communications électroniques : génère le PDF du
    gabarit pour le bail et l'ARCHIVE au dossier. Appelé best-effort à la
    création d'un bail (create_bail, convertir_dossier).

    ⚠️ N'ENVOIE RIEN. Règle posée par Phil le 2026-07-10 et réaffirmée le
    2026-08-12 : AUCUN courriel ne part vers un locataire sans qu'il l'ait
    déclenché lui-même. La v17b (2026-08-11) expédiait ce document pour
    signature dès la création du bail — régression corrigée ici. Le document
    reste prêt au dossier ; l'envoi se fait par le bouton « Envoyer pour
    signature » de la fiche, quand Phil le décide.
    """
    from app.api.v1.endpoints.immobilier_documents import save_document

    bail = await db.get(Bail, bail_id)
    if bail is None:
        return False
    ctx = await _build_ctx_from_bail(db, bail, TalFormRequest())
    pdf = generate_tal_pdf(
        "consentement_communications",
        ctx,
        gabarit=await _gabarit_override("consentement_communications"),
    )
    logement = await db.get(Logement, bail.logement_id)
    label, _desc = _TAL_LABELS["consentement_communications"]
    await save_document(
        db,
        bail_id=bail.id,
        locataire_id=bail.locataire_id,
        immeuble_id=logement.immeuble_id if logement else None,
        doc_type="consentement_communications",
        titre=label,
        params={},
        pdf=pdf,
        created_by_email=getattr(user, "email", None),
    )
    await db.commit()
    return True


class EnvoyerConsentementResult(BaseModel):
    document_id: int
    envoye_a: str
    deja_signe: bool = False


@router.post(
    "/baux/{bail_id}/consentement/envoyer",
    response_model=EnvoyerConsentementResult,
)
async def envoyer_consentement(
    bail_id: int, db: DBSession, user: CurrentUser
) -> EnvoyerConsentementResult:
    """Envoie au locataire le consentement aux communications
    électroniques, en le PRÉPARANT au besoin.

    Retour Phil 2026-08-19 : « je pouvais juste l'envoyer à partir de la
    section documents de la fiche d'un locataire, ce qui est pas bon du
    tout… là ça va tomber entre les craques ». D'où une action unique,
    appelable depuis n'importe où : juste après l'import du bail signé
    (le moment qu'il a nommé), depuis le suivi des consentements, ou
    depuis la fiche.

    Idempotent au sens utile : un consentement DÉJÀ SIGNÉ n'est pas
    renvoyé — on le signale plutôt que de redemander au locataire ce
    qu'il a déjà accordé.
    """
    _require_volet(user)
    from app.api.v1.endpoints.immobilier_documents import (
        EnvoyerSignatureRequest,
        envoyer_signature,
    )

    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")

    doc = (
        await db.execute(
            select(ImmDocument)
            .where(
                ImmDocument.bail_id == bail_id,
                ImmDocument.type == "consentement_communications",
            )
            .order_by(ImmDocument.id.desc())
        )
    ).scalars().first()

    if doc is not None and doc.signed_at is not None:
        return EnvoyerConsentementResult(
            document_id=doc.id,
            envoye_a=(doc.envoye_a or ""),
            deja_signe=True,
        )

    if doc is None:
        # Jamais préparé (bail ancien, import après un achat d'immeuble) :
        # on le génère à la volée plutôt que d'exiger un détour.
        await preparer_consentement_communications(db, bail_id, user)
        doc = (
            await db.execute(
                select(ImmDocument)
                .where(
                    ImmDocument.bail_id == bail_id,
                    ImmDocument.type == "consentement_communications",
                )
                .order_by(ImmDocument.id.desc())
            )
        ).scalars().first()
    if doc is None:
        raise HTTPException(
            status_code=500,
            detail="Impossible de préparer le consentement.",
        )

    res = await envoyer_signature(
        doc_id=doc.id,
        payload=EnvoyerSignatureRequest(),
        db=db,
        user=user,
    )
    return EnvoyerConsentementResult(
        document_id=res.document_id, envoye_a=res.envoye_a
    )


# ─── Renouvellements ──────────────────────────────────────────────────


@router.post(
    "/baux/{bail_id}/envoyer-renouvellement",
    response_model=EnvoyerRenouvellementResult,
)
async def envoyer_renouvellement(
    bail_id: int,
    payload: EnvoyerRenouvellementRequest,
    db: DBSession,
    user: CurrentUser,
) -> EnvoyerRenouvellementResult:
    """Génère + envoie l'avis de renouvellement pour un bail donné.

    Supporte hausse absolue, hausse % ou hausse $ (priorité dans cet
    ordre). Avec `request_read_receipt`, demande l'accusé de lecture
    Microsoft Graph + BCC à l'expéditeur (= envoi certifié pratique).
    """
    _require_volet(user)
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    # Garde-fou (audit 2026-07-31) : pas d'avis de renouvellement sur
    # un bail résilié/terminé/proposé.
    if bail.status != BailStatus.ACTIF.value:
        raise HTTPException(
            status_code=400,
            detail="Ce bail n'est pas actif — aucun avis à envoyer.",
        )

    # Calcul du nouveau loyer selon le mode choisi — TOUJOURS arrondi
    # au dollar supérieur (retour Phil 2026-07-30). Le loyer actuel
    # peut être corrigé depuis la fiche (retour Phil 2026-07-31).
    courant = (
        float(payload.loyer_actuel)
        if payload.loyer_actuel is not None
        else (float(bail.loyer_mensuel) if bail.loyer_mensuel else 0.0)
    )
    nouveau = payload.nouveau_loyer
    if nouveau is None and payload.hausse_pct is not None:
        nouveau = courant * (1 + payload.hausse_pct / 100.0)
    elif nouveau is None and payload.hausse_montant is not None:
        nouveau = courant + payload.hausse_montant
    if nouveau is not None:
        nouveau = arrondir_loyer(nouveau)

    obj, sent, raison, expediteur = await send_renouvellement_for_bail(
        db,
        bail,
        nouveau_loyer=nouveau,
        nouvelle_date_debut=payload.nouvelle_date_debut,
        nouvelle_date_fin=payload.nouvelle_date_fin,
        motif=payload.motif,
        force=payload.force,
        request_read_receipt=payload.request_read_receipt,
        bcc_to_sender=payload.bcc_to_sender,
        locataire_nom=payload.locataire_nom,
        logement_adresse=payload.logement_adresse,
        loyer_actuel=payload.loyer_actuel,
    )
    await db.commit()
    return EnvoyerRenouvellementResult(
        renouvellement_id=obj.id,
        courriel_envoye=sent,
        erreur_envoi=None if sent else raison,
        expediteur=expediteur,
        avis_envoye_le=obj.avis_envoye_le,
        nouveau_loyer=float(obj.nouveau_loyer) if obj.nouveau_loyer else None,
        nouvelle_date_debut=obj.nouvelle_date_debut,
        nouvelle_date_fin=obj.nouvelle_date_fin,
    )


@router.delete(
    "/renouvellements/{renouvellement_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_renouvellement(
    renouvellement_id: int,
    db: DBSession,
    user: CurrentUser,
    force: bool = False,
) -> None:
    """Supprime un avis de renouvellement ET les documents d'avis du
    cycle (non signés) : la ligne redevient « à préparer » dans Suivis
    annuels. Miroir de DELETE /documents/{id} — retour Phil 2026-07-30.
    Refusé (409) si l'avis a été signé par le locataire."""
    _require_volet(user)
    r = await db.get(BailRenouvellement, renouvellement_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Avis introuvable.")
    # Documents d'avis du CYCLE COURANT (créés depuis l'envoi de cet
    # avis) — ceux des années passées ne sont pas touchés.
    docs = (
        await db.execute(
            select(ImmDocument).where(
                ImmDocument.bail_id == r.bail_id,
                ImmDocument.type == "avis_modification",
            )
        )
    ).scalars().all()
    du_cycle = [
        d
        for d in docs
        if d.created_at is None
        or r.avis_envoye_le is None
        or d.created_at.date() >= r.avis_envoye_le
    ]
    if any(d.signed_at is not None for d in du_cycle) and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "L'avis a été SIGNÉ par le locataire — repasse avec "
                "force=true pour le supprimer malgré tout (la preuve "
                "de signature sera perdue)."
            ),
        )
    # Annuler une RECONDUCTION tacite remet aussi la date de fin du
    # bail (elle avait été étirée d'un an).
    if r.status == "reconduit" and r.nouvelle_date_debut is not None:
        bail = await db.get(Bail, r.bail_id)
        if bail is not None:
            bail.date_fin = r.nouvelle_date_debut - timedelta(days=1)
    for d in du_cycle:
        await db.delete(d)
    await db.delete(r)
    await db.commit()


# « scan-batch » (envoi en LOT des avis par défaut) retiré — demande
# Phil 2026-07-10 : les avis partent un par un, via le bouton du bail,
# avec un contenu vérifié. Rien d'automatique ni de masse.


class ReconduireIn(BaseModel):
    #: Date de fin choisie — défaut : même jour un an plus tard.
    nouvelle_date_fin: Optional[date] = None


class ReconduireResult(BaseModel):
    bail_id: int
    ancienne_date_fin: date
    nouvelle_date_fin: date


def _plus_un_mois(d: date) -> date:
    """Même jour le mois suivant — le délai légal « d'un mois »
    (art. 1945 / 1947 C.c.Q.) ; 31 → dernier jour d'un mois court."""
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, 28)


def _plus_un_an(d: date) -> date:
    """Même jour l'année suivante (29 février → 28 février)."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


@router.post(
    "/baux/{bail_id}/reconduire", response_model=ReconduireResult
)
async def reconduire_bail(
    bail_id: int,
    db: DBSession,
    user: CurrentUser,
    payload: Optional[ReconduireIn] = None,
) -> ReconduireResult:
    """RECONDUCTION TACITE : le bail s'étire d'un an tel quel, sans avis
    (retour Phil 2026-07-28 : « une année je ne les augmente pas, le bail
    s'étire tout seul »). Ne touche ni la date de début (l'historique des
    soldes en dépend), ni le loyer, ni les inclusions — seulement la date
    de fin, +1 an. La ligne reste dans le suivi, marquée « reconduit »."""
    _require_volet(user)
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    if bail.status != BailStatus.ACTIF.value:
        raise HTTPException(
            status_code=400,
            detail="Seul un bail actif peut être reconduit.",
        )
    ancienne = bail.date_fin
    voulu = payload.nouvelle_date_fin if payload else None
    if voulu is not None and voulu <= ancienne:
        raise HTTPException(
            status_code=422,
            detail="La nouvelle date de fin doit être après la fin "
            "actuelle du bail.",
        )
    bail.date_fin = voulu or _plus_un_an(bail.date_fin)
    # Trace de la reconduction (retour Phil 2026-07-30 : « il a juste
    # disparu de la liste ») : un cycle status « reconduit » garde la
    # ligne visible, en vert, même loyer + nouvelle date. La poubelle
    # de la ligne annule la reconduction (date remise).
    db.add(
        BailRenouvellement(
            bail_id=bail.id,
            avis_envoye_le=date.today(),
            nouveau_loyer=bail.loyer_mensuel,
            nouvelle_date_debut=ancienne + timedelta(days=1),
            nouvelle_date_fin=bail.date_fin,
            status="reconduit",
            notes="Reconduction tacite — même loyer, sans avis.",
        )
    )
    await db.commit()
    log.info(
        "Bail %s reconduit tel quel par %s : %s → %s",
        bail_id, user.email, ancienne, bail.date_fin,
    )
    return ReconduireResult(
        bail_id=bail_id,
        ancienne_date_fin=ancienne,
        nouvelle_date_fin=bail.date_fin,
    )


class RenouvellementPatchIn(BaseModel):
    #: Réponse saisie à la MAIN (appel, papier) ou entente négociée.
    status: Optional[str] = Field(
        default=None,
        pattern=r"^(propose|accepte|refuse|en_negociation|depart)$",
    )
    nouveau_loyer: Optional[float] = Field(default=None, ge=0)
    refus_motif: Optional[str] = Field(default=None, max_length=2000)


class RenouvellementPatchOut(BaseModel):
    id: int
    status: str
    nouveau_loyer: Optional[float] = None
    reponse_le: Optional[date] = None


@router.patch(
    "/renouvellements/{renouvellement_id}",
    response_model=RenouvellementPatchOut,
)
async def patch_renouvellement(
    renouvellement_id: int,
    payload: RenouvellementPatchIn,
    db: DBSession,
    user: CurrentUser,
) -> RenouvellementPatchOut:
    """Réponse du locataire saisie manuellement, ou ENTENTE négociée
    (montant révisé — souvent à la baisse) : le loyer convenu sera
    reporté sur le bail à la date effective du renouvellement."""
    _require_volet(user)
    r = await db.get(BailRenouvellement, renouvellement_id)
    if r is None:
        raise HTTPException(
            status_code=404, detail="Renouvellement introuvable."
        )
    data = payload.model_dump(exclude_unset=True)
    if data.get("status"):
        r.status = data["status"]
        if r.status in ("accepte", "refuse"):
            r.reponse_le = date.today()
        elif r.status == "depart":
            # « Le locataire quitte » : la réponse est datée ET le cycle
            # de départ unifié s'enclenche (bail fermé à sa date de fin,
            # dossier de relocation, logement recalé) — 2026-08-13.
            r.reponse_le = date.today()
            from app.services.locatif_depart import declarer_depart

            await declarer_depart(
                db,
                r.bail_id,
                source=(
                    "Réponse « le locataire quitte » — suivi des "
                    "renouvellements"
                ),
            )
    if data.get("nouveau_loyer") is not None:
        r.nouveau_loyer = data["nouveau_loyer"]
        # Montant révisé → (re)reporté sur le bail à la date effective.
        r.applique_le = None
    if "refus_motif" in data:
        r.refus_motif = data["refus_motif"]
    await db.commit()
    return RenouvellementPatchOut(
        id=r.id,
        status=r.status,
        nouveau_loyer=(
            float(r.nouveau_loyer) if r.nouveau_loyer is not None else None
        ),
        reponse_le=r.reponse_le,
    )


class ResilierIn(BaseModel):
    #: Date de fin convenue (entente de départ) ou constatée
    #: (déguerpissement).
    date_fin: date
    ouvrir_relocation: bool = True
    #: v15 — transmettre au locataire un AVIS de fin de bail (lettre
    #: PDF par courriel) ; False = fin immédiate sans avis.
    envoyer_avis: bool = False


class ResilierResult(BaseModel):
    bail_id: int
    date_fin: date
    statut: str
    avis_envoye: bool = False
    avis_erreur: Optional[str] = None
    relocation_ouverte: bool = False




def _pdf_avis_resiliation(
    locataire_nom: str, adresse: str, logement_numero: str, date_fin: date
) -> bytes:
    """« Entente de résiliation de bail » (résiliation d'un commun
    accord — art. 1971 C.c.Q.) : PDF transmis pour SIGNATURE EN LIGNE
    avec suivi d'ouverture (v16)."""
    import io as _io

    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as _canvas

    buf = _io.BytesIO()
    c = _canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    x = 25 * mm
    y = h - 30 * mm
    c.setFont("Helvetica-Bold", 16)
    c.drawString(x, y, "Entente de résiliation de bail")
    y -= 6 * mm
    c.setFont("Helvetica", 10)
    c.setFillGray(0.35)
    c.drawString(
        x, y, "Résiliation d'un commun accord — Horizon Services Immobiliers"
    )
    c.setFillGray(0.0)
    y -= 4 * mm
    c.setLineWidth(0.8)
    c.line(x, y, w - 25 * mm, y)
    y -= 12 * mm
    c.setFont("Helvetica", 11)
    for t in (
        f"Locataire : {locataire_nom or '—'}",
        f"Logement : {logement_numero or '—'} — {adresse or '—'}",
        "",
        "Les parties conviennent de mettre fin au bail du logement",
        f"ci-dessus le {date_fin.isoformat()} (résiliation d'un commun",
        "accord — art. 1971 C.c.Q.).",
        "",
        "Le logement devra être libéré et remis en bon état à cette date.",
        "Le dépôt et les ajustements finaux, s'il y a lieu, seront traités",
        "après l'état des lieux.",
        "",
        "La signature du locataire se fait EN LIGNE via le lien reçu par",
        "courriel — elle vaut acceptation de la présente entente.",
        "",
        "Horizon Services Immobiliers",
    ):
        c.drawString(x, y, t)
        y -= 7 * mm
    c.showPage()
    c.save()
    return buf.getvalue()


async def _envoyer_entente_resiliation(db, bail, date_fin: date, user) -> bool:
    """Génère l'ENTENTE de résiliation (PDF au dossier, date de fin
    dans les params) et l'envoie pour SIGNATURE EN LIGNE — suivi
    d'ouverture et de signature comme les avis de renouvellement. À la
    signature, public_document résilie le bail et ouvre la relocation."""
    from app.api.v1.endpoints.immobilier_documents import (
        EnvoyerSignatureRequest,
        envoyer_signature,
        save_document,
    )
    from app.models.immobilier import Immeuble, Locataire, Logement

    locataire = await db.get(Locataire, bail.locataire_id)
    if locataire is None or not (locataire.email or "").strip():
        raise RuntimeError(
            "Le locataire n'a pas de courriel — entente non transmise."
        )
    lg = await db.get(Logement, bail.logement_id)
    im = await db.get(Immeuble, lg.immeuble_id) if lg else None
    adresse = (
        f"{im.address}, {im.city}" if im and im.city else
        (im.address if im else "")
    )
    pdf = _pdf_avis_resiliation(
        locataire.full_name, adresse, lg.numero if lg else "", date_fin
    )
    doc = await save_document(
        db,
        bail_id=bail.id,
        locataire_id=locataire.id,
        immeuble_id=im.id if im else None,
        doc_type="avis_resiliation",
        titre=f"Entente de résiliation — fin le {date_fin.isoformat()}",
        params={"date_fin": date_fin.isoformat()},
        pdf=pdf,
        created_by_email=getattr(user, "email", None),
    )
    await db.flush()
    # Envoi pour SIGNATURE (page publique + suivi ouvert/signé).
    await envoyer_signature(
        doc_id=doc.id,
        payload=EnvoyerSignatureRequest(),
        db=db,
        user=user,
    )
    return True


class AnnulerDepartResult(BaseModel):
    bail_id: int
    dossier_annule_id: Optional[int] = None
    bail_reactive: bool = False
    logement_statut: Optional[str] = None


@router.post(
    "/baux/{bail_id}/annuler-depart", response_model=AnnulerDepartResult
)
async def annuler_depart(
    bail_id: int, db: DBSession, user: CurrentUser
) -> AnnulerDepartResult:
    """Annule un DÉPART confirmé : le locataire reste.

    Retour Phil 2026-08-19 : sur un bail dont le départ est déjà acté,
    « Mettre fin au bail » restait proposé — geste sans effet qui laisse
    croire à une action. L'action utile à ce moment-là est l'INVERSE :
    revenir en arrière parce que le locataire a changé d'idée.

    Garde-fou : dès qu'un LOCATAIRE est lié pour la suite (bail en
    signature), annuler n'est plus anodin — on aurait deux locataires
    sur le même logement. Le refus est explicite et dit quoi faire
    (retirer le locataire, puis annuler le dossier de relocation).
    """
    _require_volet(user)
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")

    from app.services.locatif_depart import (
        dossier_relocation_actif,
        recaler_statut_logement,
    )

    dossier = await dossier_relocation_actif(db, bail.logement_id)
    if dossier is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Aucun départ en cours sur ce logement.",
        )

    engages = (
        LocationDossierStatut.BAIL_ENVOYE.value,
        "bail_a_envoyer",  # ancienne étape, avant migration
        "candidat_retenu",
    )
    if dossier.statut in engages or dossier.nouveau_bail_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Un locataire est déjà lié à ce logement (bail en "
                "signature) — annuler le départ mettrait deux locataires "
                "sur la même unité. Retire d'abord le locataire dans la "
                "page Locations, puis annule le dossier de relocation."
            ),
        )

    dossier.statut = LocationDossierStatut.ANNULE.value
    dossier.updated_at = datetime.now(timezone.utc)
    note = (dossier.notes or "").strip()
    marque = (
        f"Départ annulé le {datetime.now(timezone.utc).date().isoformat()} par "
        f"{getattr(user, 'email', None) or 'un gestionnaire'} — le "
        "locataire reste."
    )
    dossier.notes = (
        (note + chr(10) + marque).strip() if note else marque
    )

    # Le bail a pu être fermé par la résiliation : on le rouvre.
    reactive = False
    if bail.status in (
        BailStatus.RESILIE.value, BailStatus.TERMINE.value
    ):
        bail.status = BailStatus.ACTIF.value
        bail.updated_at = datetime.now(timezone.utc)
        reactive = True

    await db.flush()
    await recaler_statut_logement(db, bail.logement_id)
    lg = await db.get(Logement, bail.logement_id)
    await db.commit()
    log.info(
        "Départ annulé sur le bail %s (dossier %s) par %s",
        bail_id, dossier.id, getattr(user, "email", None),
    )
    return AnnulerDepartResult(
        bail_id=bail.id,
        dossier_annule_id=dossier.id,
        bail_reactive=reactive,
        logement_statut=(lg.status if lg else None),
    )


@router.post("/baux/{bail_id}/resilier", response_model=ResilierResult)
async def resilier_bail(
    bail_id: int,
    payload: ResilierIn,
    db: DBSession,
    user: CurrentUser,
) -> ResilierResult:
    """RÉSILIATION avant terme (entente de départ, déguerpissement —
    art. 1975 C.c.Q.) : le bail passe « résilié » à la date convenue,
    l'historique des paiements est conservé, et un dossier de
    relocation s'ouvre au besoin (retour Phil 2026-07-31 : « pouvoir
    le résilier — dans la section bail »)."""
    _require_volet(user)
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    if bail.status != BailStatus.ACTIF.value:
        raise HTTPException(
            status_code=400,
            detail="Seul un bail actif peut être résilié.",
        )
    # v16 — mode ENTENTE : rien ne se résilie tout de suite. L'entente
    # part pour signature en ligne ; la page Baux passe la ligne en
    # ROUGE « résiliation en cours » ; à la signature du locataire, le
    # bail se résilie à la date convenue et la relocation s'ouvre
    # (hook public_document).
    if payload.envoyer_avis:
        try:
            await _envoyer_entente_resiliation(
                db, bail, payload.date_fin, user
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Envoi de l'entente échoué : {exc}",
            )
        await db.commit()
        log.info(
            "Entente de résiliation envoyée (bail %s, fin %s) par %s",
            bail_id, payload.date_fin, user.email,
        )
        return ResilierResult(
            bail_id=bail_id,
            date_fin=payload.date_fin,
            statut="resiliation_en_cours",
            relocation_ouverte=False,
            avis_envoye=True,
        )
    # Cycle unifié (2026-08-13) : TOUT passe par le service —
    # fermeture du bail (résilié si la date est passée, sinon fin posée
    # et recalage lazy à l'échéance), recalage du logement, dossier de
    # relocation créé/complété, cycle de renouvellement → « depart ».
    from app.services.locatif_depart import (
        declarer_depart,
        dossier_relocation_actif,
    )

    avait_dossier = (
        bail.logement_id is not None
        and await dossier_relocation_actif(db, bail.logement_id) is not None
    )
    await declarer_depart(
        db,
        bail_id,
        date_depart=payload.date_fin,
        source="Résiliation immédiate",
        ouvrir_dossier=payload.ouvrir_relocation,
    )
    await db.commit()
    log.info(
        "Bail %s résilié au %s par %s",
        bail_id, payload.date_fin, user.email,
    )
    return ResilierResult(
        bail_id=bail_id,
        date_fin=payload.date_fin,
        statut=bail.status,
        relocation_ouverte=payload.ouvrir_relocation and not avait_dossier,
    )


# ─── Transfert d'unité (points 11-12, retours Phil 2026-09-09) ──────────


class TransfererIn(BaseModel):
    """Le locataire change de logement EN UN GESTE : son bail actuel se
    termine la veille, un NOUVEAU bail (obligatoire — proposé, à signer)
    est créé sur la nouvelle unité, et le dépôt de garantie SUIT le
    locataire (par défaut). Tout est modifiable avant confirmation."""

    nouveau_logement_id: int
    date_transfert: date
    #: Fin du nouveau bail — défaut : la fin du bail actuel si elle est
    #: encore devant, sinon le prochain 30 juin « utile ».
    date_fin: Optional[date] = None
    loyer_mensuel: float = Field(..., ge=0)
    #: Le dépôt de l'ancien bail suit le locataire (rien à rendre, rien
    #: à re-percevoir). False → ``depot_garantie`` devient le dépôt du
    #: nouveau bail et l'ancien reste « à rendre ».
    transferer_depot: bool = True
    depot_garantie: Optional[float] = Field(default=None, ge=0)
    au_mois: Optional[bool] = None
    notes: Optional[str] = Field(default=None, max_length=2000)


class TransfererResult(BaseModel):
    ancien_bail_id: int
    ancien_bail_fin: date
    nouveau_bail_id: int
    nouveau_logement_id: int
    nouveau_logement_numero: Optional[str] = None
    immeuble_id: int
    dossier_id: Optional[int] = None
    depot_transfere: float = 0.0


def _prochain_30_juin_utile(depuis: date) -> date:
    """Prochain 30 juin avec au moins ~3 mois de bail (même règle que le
    formulaire « Assigner un bail »)."""
    annee = depuis.year + (1 if depuis.month >= 4 else 0)
    return date(annee, 6, 30)


@router.post(
    "/baux/{bail_id}/transferer",
    response_model=TransfererResult,
    status_code=status.HTTP_201_CREATED,
)
async def transferer_unite(
    bail_id: int,
    payload: TransfererIn,
    db: DBSession,
    user: CurrentUser,
) -> TransfererResult:
    """TRANSFERT D'UNITÉ (retour Phil 2026-09-09) : « il y a effectivement
    un nouveau bail s'il change d'unité ». En un geste :

    1. le bail actuel se termine la VEILLE du transfert (cycle unifié
       ``declarer_depart`` : résilié si la date est passée, sinon fin
       posée ; dossier de relocation ouvert sur l'ancienne unité) ;
    2. un NOUVEAU bail « proposé » est créé sur la nouvelle unité pour
       le même locataire — le bail signé (PDF du gestionnaire, ou notre
       futur système) s'y joint ensuite et le rend actif ;
    3. le dépôt de garantie SUIT le locataire (``depot_transfere_vers_
       bail_id`` sur l'ancien bail : rien à rendre, rien de rendu) ;
    4. la nouvelle unité passe « bail en signature » au kanban.
    """
    _require_volet(user)
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    if bail.status != BailStatus.ACTIF.value:
        raise HTTPException(
            status_code=400,
            detail="Seul un bail actif peut être transféré.",
        )
    if payload.nouveau_logement_id == bail.logement_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choisis un AUTRE logement que celui du bail actuel.",
        )
    nouveau = await db.get(Logement, payload.nouveau_logement_id)
    if nouveau is None:
        raise HTTPException(status_code=404, detail="Logement introuvable.")
    ancien_lg = await db.get(Logement, bail.logement_id)

    from app.services.gestion_externe import (
        erreur_externe,
        immeuble_est_externe,
        logement_est_externe,
    )

    if await immeuble_est_externe(db, nouveau.immeuble_id):
        raise erreur_externe(
            "le transfert vers un immeuble en gestion externe se règle "
            "chez le gestionnaire."
        )
    if await logement_est_externe(db, bail.logement_id):
        raise erreur_externe("pas de bail dans Kratos à transférer.")

    date_transfert = payload.date_transfert
    veille = date_transfert - timedelta(days=1)
    if veille < bail.date_debut:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La date de transfert doit être après le début du bail actuel.",
        )
    date_fin = payload.date_fin
    if date_fin is None:
        date_fin = (
            bail.date_fin
            if bail.date_fin is not None and bail.date_fin > date_transfert
            else _prochain_30_juin_utile(date_transfert)
        )
    if date_fin <= date_transfert:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La fin du nouveau bail doit être après la date de transfert.",
        )

    # Disponibilité de la nouvelle unité sur la période : un bail actif
    # ou proposé qui la chevauche (un bail au mois court sans égard à sa
    # date de fin) → refus explicite.
    occupants = (
        await db.execute(
            select(Bail).where(
                Bail.logement_id == nouveau.id,
                Bail.id != bail.id,
                Bail.status.in_(
                    [BailStatus.ACTIF.value, BailStatus.PROPOSE.value]
                ),
            )
        )
    ).scalars().all()
    for o in occupants:
        chevauche = bool(o.au_mois) or (
            o.date_fin is not None
            and o.date_fin >= date_transfert
            and o.date_debut is not None
            and o.date_debut <= date_fin
        )
        if chevauche:
            lo_o = await db.get(Locataire, o.locataire_id)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Le logement {nouveau.numero} est déjà loué ou réservé "
                    f"sur cette période ({lo_o.full_name if lo_o else 'bail #' + str(o.id)}). "
                    "Choisis une autre unité ou une autre date."
                ),
            )
    # Un seul transfert à la fois : un bail proposé du même locataire
    # ailleurs veut dire qu'un transfert (ou une relocation) est déjà en
    # cours — on ne fabrique pas deux baux en attente.
    autre = (
        await db.execute(
            select(Bail).where(
                Bail.locataire_id == bail.locataire_id,
                Bail.id != bail.id,
                Bail.status == BailStatus.PROPOSE.value,
            )
        )
    ).scalars().first()
    if autre is not None:
        lg_a = await db.get(Logement, autre.logement_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Un bail proposé existe déjà pour ce locataire (logement "
                f"{lg_a.numero if lg_a else autre.logement_id}) — joins "
                "son bail signé ou retire-le avant un transfert."
            ),
        )

    now = datetime.now(timezone.utc)
    depot_actuel = float(bail.depot_garantie or 0)
    transfere = bool(payload.transferer_depot and depot_actuel > 0)
    note_auto = (
        f"Transfert d'unité depuis le logement "
        f"{ancien_lg.numero if ancien_lg else bail.logement_id} "
        f"(bail #{bail.id}) le {date_transfert.isoformat()}."
    )
    notes = note_auto + (
        (chr(10) + payload.notes.strip()) if (payload.notes or "").strip() else ""
    )
    nb = Bail(
        logement_id=nouveau.id,
        locataire_id=bail.locataire_id,
        date_debut=date_transfert,
        date_fin=date_fin,
        loyer_mensuel=payload.loyer_mensuel,
        depot_garantie=(depot_actuel if transfere else payload.depot_garantie),
        depot_recu_le=(bail.depot_recu_le if transfere else None),
        depot_detenteur=(bail.depot_detenteur if transfere else None),
        status=BailStatus.PROPOSE.value,
        au_mois=(
            payload.au_mois
            if payload.au_mois is not None
            else (
                True
                if getattr(nouveau, "location_en_chambres", False)
                else bail.au_mois
            )
        ),
        jour_echeance=bail.jour_echeance or 1,
        chauffage_inclus=bool(bail.chauffage_inclus),
        eau_chaude_inclus=bool(bail.eau_chaude_inclus),
        electricite_inclus=bool(bail.electricite_inclus),
        internet_inclus=bool(bail.internet_inclus),
        notes=notes,
    )
    nb.created_at = now
    nb.updated_at = now
    db.add(nb)
    await db.flush()
    if transfere:
        bail.depot_transfere_vers_bail_id = nb.id
        bail.updated_at = now

    # 1) L'ancien bail se termine la veille — cycle unifié (relocation
    #    de l'ancienne unité ouverte, logement recalé, cycle de
    #    renouvellement → départ).
    from app.services.locatif_depart import (
        declarer_depart,
        dossier_relocation_actif,
        marquer_prise_en_charge_humaine,
        recaler_statut_logement,
    )

    await declarer_depart(
        db,
        bail.id,
        date_depart=veille,
        source=f"Transfert d'unité vers le logement {nouveau.numero}",
        ouvrir_dossier=True,
    )

    # 2) La nouvelle unité passe « bail en signature » au kanban : le
    #    dossier de relocation existant (unité vacante) est repris,
    #    sinon créé — l'import du bail signé le refermera (« reloué »).
    dossier = await dossier_relocation_actif(db, nouveau.id)
    if dossier is None:
        dossier = LocationDossier(
            logement_id=nouveau.id,
            statut=LocationDossierStatut.BAIL_ENVOYE.value,
            loyer_demande=payload.loyer_mensuel,
            notes="Créé automatiquement — transfert d'unité.",
        )
        dossier.created_at = now
        dossier.updated_at = now
        db.add(dossier)
        await db.flush()
    if dossier.nouveau_bail_id is None:
        dossier.nouveau_bail_id = nb.id
    dossier.statut = LocationDossierStatut.BAIL_ENVOYE.value
    dossier.updated_at = now
    marquer_prise_en_charge_humaine(dossier)
    await recaler_statut_logement(db, nouveau.id)

    await db.commit()
    log.info(
        "Transfert d'unité : bail %s → bail %s (logement %s → %s) par %s",
        bail.id, nb.id, bail.logement_id, nouveau.id, user.email,
    )
    return TransfererResult(
        ancien_bail_id=bail.id,
        ancien_bail_fin=veille,
        nouveau_bail_id=nb.id,
        nouveau_logement_id=nouveau.id,
        nouveau_logement_numero=nouveau.numero,
        immeuble_id=nouveau.immeuble_id,
        dossier_id=dossier.id,
        depot_transfere=(depot_actuel if transfere else 0.0),
    )


@router.get(
    "/renouvellements/overview",
    response_model=List[RenouvellementOverview],
)
async def renouvellements_overview(
    db: DBSession,
    user: CurrentUser,
    locataire_id: Optional[int] = None,
    logement_id: Optional[int] = None,
) -> List[RenouvellementOverview]:
    """Liste TOUS les baux actifs avec leur statut de renouvellement
    (retour Phil 2026-07-30 : tous les locataires restent visibles —
    un bail reconduit ou dont la fin est loin apparaît quand même).

    ``locataire_id`` / ``logement_id`` restreignent la MÊME liste à une
    fiche : « ça doit être exactement pareil que dans la page, mais
    juste pour ce locataire-là » (Phil 2026-08-19). Un filtre, jamais
    une deuxième implémentation."""
    _require_volet(user)
    from app.services.locatif_suivis import get_suivis

    suivis_cfg = await get_suivis()
    today = date.today()

    # Gestion externe : les renouvellements relèvent du gestionnaire
    # tiers → exclu (isnot(True) couvre les NULL legacy).
    # Borne basse : les baux ÉCHUS depuis moins d'un an restent visibles
    # (badge « Bail échu ») au lieu de disparaître silencieusement — la
    # date doit être corrigée (retour Phil 2026-07-28).
    bails = (
        await db.execute(
            select(Bail)
            .join(Logement, Logement.id == Bail.logement_id)
            .join(Immeuble, Immeuble.id == Logement.immeuble_id)
            .where(
                and_(
                    Bail.status == BailStatus.ACTIF.value,
                    Bail.date_fin >= today - timedelta(days=365),
                    Immeuble.gestion_externe.isnot(True),
                    # Baux AU MOIS : reconduction auto, jamais d'avis —
                    # hors du suivi (retour Phil 2026-07-28).
                    Bail.au_mois.isnot(True),
                    # « Louer indéfiniment (chambre) » : même logique au
                    # niveau du LOGEMENT — filet pour les baux legacy
                    # créés avant que le flag pilote au_mois (2026-08-13).
                    Logement.location_en_chambres.isnot(True),
                )
            ).order_by(Bail.date_fin.asc())
        )
    ).scalars().all()

    # Reconduction tacite AUTOMATIQUE (lazy, 2026-08-13) : un bail échu
    # sans réponse est reconduit tel quel ; un bail échu dont le départ
    # était annoncé (dossier de relocation actif) est terminé — dans les
    # deux cas la liste reflète l'état recalé.
    from app.services.locatif_depart import reconduire_tacitement_baux_echus

    if await reconduire_tacitement_baux_echus(db, bails):
        bails = [b for b in bails if b.status == BailStatus.ACTIF.value]

    # Chargement groupé pour éviter le N+1 (auparavant 3 db.get + 1 select par
    # bail). On collecte les identifiants puis on résout logements, immeubles,
    # locataires et le dernier renouvellement par bail via des requêtes in_().
    # Le contenu et l'ordre de la réponse restent identiques : la boucle ci-
    # dessous consomme les mêmes objets, résolus depuis des dicts.
    log_ids = {b.logement_id for b in bails if b.logement_id}
    log_by_id: dict = {}
    if log_ids:
        log_by_id = {
            lg.id: lg
            for lg in (
                await db.execute(
                    select(Logement).where(Logement.id.in_(list(log_ids)))
                )
            ).scalars().all()
        }

    imm_ids = {lg.immeuble_id for lg in log_by_id.values() if lg.immeuble_id}
    imm_by_id: dict = {}
    if imm_ids:
        imm_by_id = {
            im.id: im
            for im in (
                await db.execute(
                    select(Immeuble).where(Immeuble.id.in_(list(imm_ids)))
                )
            ).scalars().all()
        }

    loc_ids = {b.locataire_id for b in bails if b.locataire_id}
    loc_by_id: dict = {}
    if loc_ids:
        loc_by_id = {
            lo.id: lo
            for lo in (
                await db.execute(
                    select(Locataire).where(Locataire.id.in_(list(loc_ids)))
                )
            ).scalars().all()
        }

    # Dernier renouvellement par bail : on charge tous les renouvellements des
    # baux visés en une requête, puis on retient celui au avis_envoye_le le plus
    # récent par bail (mêmes semantiques que ORDER BY avis_envoye_le DESC LIMIT
    # 1 de la version précédente ; avis_envoye_le est NOT NULL, donc pas de cas
    # NULL ; on départage un ex-æquo par id décroissant, le plus récent créé).
    last_ren_by_bail: dict = {}
    bail_ids = [b.id for b in bails]
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

    # Dossier de RELOCATION actif (non annulé) par logement : la ligne
    # est bloquée tant que le dossier vit (retour Phil 2026-07-31).
    reloc_by_logement: dict = {}
    if log_by_id:
        from app.models.immobilier import LocationDossier

        for dossier in (
            await db.execute(
                select(LocationDossier).where(
                    LocationDossier.logement_id.in_(list(log_by_id.keys())),
                    LocationDossier.statut != "annule",
                )
            )
        ).scalars().all():
            cur = reloc_by_logement.get(dossier.logement_id)
            if cur is None or dossier.id > cur.id:
                reloc_by_logement[dossier.logement_id] = dossier

    # Dernier DOCUMENT d'avis (TAL-806) par bail → suivi envoyé/ouvert/
    # signé directement sur la page Renouvellements (retour Phil
    # 2026-07-20, point 11). Tri ascendant → le plus récent écrase.
    last_doc_by_bail: dict = {}
    if bail_ids:
        for d in (
            await db.execute(
                select(ImmDocument)
                .where(
                    ImmDocument.bail_id.in_(bail_ids),
                    ImmDocument.type == "avis_modification",
                )
                .order_by(ImmDocument.created_at.asc(), ImmDocument.id.asc())
            )
        ).scalars().all():
            last_doc_by_bail[d.bail_id] = d

    out: List[RenouvellementOverview] = []
    dirty = False
    for b in bails:
        logement = log_by_id.get(b.logement_id)
        immeuble = (
            imm_by_id.get(logement.immeuble_id) if logement else None
        )
        locataire = loc_by_id.get(b.locataire_id)
        last_ren = last_ren_by_bail.get(b.id)

        # v3 (2026-07-30) — machine à états du renouvellement.
        # « Cycle courant » = même borne que l'idempotence d'envoi : un
        # avis d'un cycle PASSÉ ne compte plus (la ligne redevient « à
        # envoyer » quand la fenêtre suivante s'ouvre).
        from app.services.bail_renouvellement import est_cycle_courant

        reconduit = last_ren is not None and last_ren.status == "reconduit"
        courant = last_ren is not None and est_cycle_courant(
            last_ren, b.date_fin, today
        )
        reponse = None
        deadline_reponse = None
        deadline_fixation = None
        from app.services.locatif_depart import DOSSIER_STATUTS_REGLES

        reloc_active = (
            b.logement_id in reloc_by_logement
            and reloc_by_logement[b.logement_id].statut
            not in DOSSIER_STATUTS_REGLES
        )
        if courant:
            deadline_reponse = _plus_un_mois(last_ren.avis_envoye_le)
            if (
                last_ren.status == "propose"
                and today > deadline_reponse
                and not reloc_active
            ):
                # Art. 1945 C.c.Q. : sans réponse dans le mois de la
                # réception, le locataire est réputé avoir ACCEPTÉ.
                # SAUF si un dossier de relocation ACTIF existe sur le
                # logement : le départ est annoncé, pas d'acceptation
                # tacite (cycle unifié 2026-08-13).
                last_ren.status = "repute_accepte"
                last_ren.reponse_le = deadline_reponse
                dirty = True
            if last_ren.status in ("accepte", "repute_accepte") and (
                last_ren.applique_le is None
                and last_ren.nouvelle_date_debut is not None
                and last_ren.nouvelle_date_debut <= today
            ):
                # Application au bail À LA DATE EFFECTIVE — lazy, à la
                # consultation (pas de cron ; rien ne part au locataire).
                if last_ren.nouveau_loyer is not None:
                    b.loyer_mensuel = last_ren.nouveau_loyer
                # La période de l'AVIS remplace celle du bail, même si
                # elle la RACCOURCIT (retour Phil 2026-07-31 : avis
                # jusqu'en 2027 sur un bail qui allait jusqu'en 2028).
                if (
                    last_ren.nouvelle_date_fin is not None
                    and last_ren.nouvelle_date_fin != b.date_fin
                ):
                    b.date_fin = last_ren.nouvelle_date_fin
                # Le « loyer demandé » suit le bail tant que c'est loué
                # (retour client 2026-08-14) : l'avis appliqué réaligne
                # le logement — 1 000 $ posé à la création ne doit pas
                # survivre à douze ans d'augmentations.
                if last_ren.nouveau_loyer is not None:
                    from app.services.loyer_effectif import (
                        refleter_bail_sur_demande,
                    )

                    lg_avis = await db.get(Logement, b.logement_id)
                    if lg_avis is not None:
                        refleter_bail_sur_demande(
                            lg_avis, float(last_ren.nouveau_loyer)
                        )
                last_ren.applique_le = today
                dirty = True
                # Cycle réglé et appliqué : dès CE rendu, la ligne
                # repart sur le suivi normal de la nouvelle période.
                courant = False
                reponse = None
                deadline_reponse = None
            if not courant:
                pass  # appliqué à l'instant — suivi normal repris
            elif last_ren.status == "refuse":
                reponse = "refuse"
                if last_ren.reponse_le is not None:
                    # Art. 1947 : 1 mois pour demander la fixation au
                    # TAL, sinon le bail se renouvelle aux ANCIENNES
                    # conditions.
                    deadline_fixation = _plus_un_mois(last_ren.reponse_le)
            elif last_ren.status == "accepte":
                reponse = "accepte"
            elif last_ren.status == "repute_accepte":
                reponse = "repute_accepte"
            elif last_ren.status == "depart":
                # Le locataire a répondu « je quitterai à la fin du
                # bail » — ouvrir un dossier de relocation.
                reponse = "depart"
            else:  # propose / en_negociation
                reponse = "attente"

        delta = (b.date_fin - today).days
        # Fenêtre « à envoyer » = jusqu'à N mois avant la fin (réglable,
        # défaut 6). Une reconduction tacite ou un avis du cycle courant
        # règlent la ligne ; sinon le suivi normal reprend.
        fenetre = suivis_cfg.fenetre_renouvellement(
            delta, avis_envoye=courant
        )
        if reconduit and fenetre == "hors_fenetre":
            fenetre = "reconduit"

        out.append(
            RenouvellementOverview(
                bail_id=b.id,
                immeuble_id=immeuble.id if immeuble else 0,
                immeuble_name=immeuble.name if immeuble else "—",
                immeuble_adresse=(
                    f"{immeuble.address}, {immeuble.city}"
                    if immeuble and immeuble.city
                    else (immeuble.address if immeuble else None)
                ),
                logement_id=logement.id if logement else None,
                logement_numero=logement.numero if logement else "—",
                locataire_id=locataire.id if locataire else None,
                locataire_nom=locataire.full_name if locataire else "—",
                locataire_email=locataire.email if locataire else None,
                bail_date_fin=b.date_fin,
                bail_loyer_mensuel=float(b.loyer_mensuel),
                jours_avant_fin=delta,
                fenetre=fenetre,
                avis_envoye_le=last_ren.avis_envoye_le if last_ren else None,
                nouveau_loyer=(
                    float(last_ren.nouveau_loyer)
                    if last_ren and last_ren.nouveau_loyer is not None
                    else None
                ),
                renouvellement_status=last_ren.status if last_ren else None,
                renouvellement_id=last_ren.id if last_ren else None,
                document_id=(
                    getattr(last_ren, "document_id", None)
                    if last_ren
                    else None
                ),
                avis_doc_envoye_le=(
                    last_doc_by_bail[b.id].envoye_le
                    if b.id in last_doc_by_bail
                    else None
                ),
                avis_doc_ouvert_le=(
                    last_doc_by_bail[b.id].ouvert_le
                    if b.id in last_doc_by_bail
                    else None
                ),
                avis_doc_signed_at=(
                    last_doc_by_bail[b.id].signed_at
                    if b.id in last_doc_by_bail
                    else None
                ),
                reponse=reponse,
                nouvelle_date_debut=(
                    last_ren.nouvelle_date_debut if courant else None
                ),
                nouvelle_date_fin=(
                    last_ren.nouvelle_date_fin if courant else None
                ),
                reponse_le=last_ren.reponse_le if last_ren else None,
                deadline_reponse=deadline_reponse,
                deadline_fixation=deadline_fixation,
                refus_motif=last_ren.refus_motif if courant else None,
                applique_le=last_ren.applique_le if last_ren else None,
                relocation_dossier_id=(
                    reloc_by_logement[b.logement_id].id
                    if b.logement_id in reloc_by_logement
                    else None
                ),
                assurance_confirmee_le=(
                    locataire.assurance_confirmee_le if locataire else None
                ),
            )
        )
    if dirty:
        # Transitions « réputé accepté » + applications au bail.
        await db.commit()
    if locataire_id is not None:
        out = [r for r in out if r.locataire_id == locataire_id]
    if logement_id is not None:
        out = [r for r in out if r.logement_id == logement_id]
    return out


# ─── Vue immobilier par entreprise propriétaire ────────────────────────


async def _compute_part_metrics(
    db, immeuble: Immeuble, ownership_pct: float
) -> tuple[int, int, float, float, float]:
    """Retourne (nb_actifs, nb_occ, revenu_part, valeur_part, balance_part)."""
    pct = ownership_pct / 100.0

    # Logements
    log_rows = (
        await db.execute(
            select(Logement.status, func.count(Logement.id))
            .where(Logement.immeuble_id == immeuble.id)
            .group_by(Logement.status)
        )
    ).all()
    sts = {st: int(n) for st, n in log_rows}
    nb_actifs = sum(
        n for st, n in sts.items() if st != LogementStatus.HORS_LOC.value
    )
    nb_occ = sts.get(LogementStatus.OCCUPE.value, 0)

    # Revenu mensuel total (Σ baux actifs)
    revenu = float(
        (
            await db.execute(
                select(func.coalesce(func.sum(Bail.loyer_mensuel), 0))
                .join(Logement, Logement.id == Bail.logement_id)
                .where(
                    and_(
                        Logement.immeuble_id == immeuble.id,
                        Bail.status == BailStatus.ACTIF.value,
                    )
                )
            )
        ).scalar()
        or 0
    )

    # Valeur immeuble : l'évaluation de référence prime, sinon la plus
    # récente, fallback municipal puis prix d'achat (même logique que
    # get_financials — l'équité doit raconter la même histoire partout).
    val = (
        await db.execute(
            select(Evaluation.valeur)
            .where(
                and_(
                    Evaluation.immeuble_id == immeuble.id,
                    Evaluation.is_reference.is_(True),
                )
            )
            .order_by(Evaluation.date_evaluation.desc())
            .limit(1)
        )
    ).scalar()
    if val is None:
        val = (
            await db.execute(
                select(Evaluation.valeur)
                .where(Evaluation.immeuble_id == immeuble.id)
                .order_by(Evaluation.date_evaluation.desc())
                .limit(1)
            )
        ).scalar()
    if val is None:
        val = (
            await db.execute(
                select(Evaluation.valeur)
                .where(
                    and_(
                        Evaluation.immeuble_id == immeuble.id,
                        Evaluation.kind == EvaluationKind.MUNICIPALE.value,
                    )
                )
                .order_by(Evaluation.date_evaluation.desc())
                .limit(1)
            )
        ).scalar()
    if val is None and immeuble.purchase_price is not None:
        val = immeuble.purchase_price
    valeur_imm = float(val) if val is not None else 0.0

    # Hypothèque active. Balance EFFECTIVE : saisie > calculée au jour J
    # (tableau d'amortissement) > montant initial — même logique que la
    # fiche immeuble.
    from app.services.hypotheque_calc import balance_effective

    balance_hyp = round(
        sum(
            balance_effective(h)
            for h in (
                await db.execute(
                    select(Hypotheque).where(
                        and_(
                            Hypotheque.immeuble_id == immeuble.id,
                            Hypotheque.status
                            == HypothequeStatus.ACTIVE.value,
                        )
                    )
                )
            ).scalars().all()
        ),
        2,
    )

    return (
        nb_actifs,
        nb_occ,
        round(revenu * pct, 2),
        round(valeur_imm * pct, 2),
        round(balance_hyp * pct, 2),
    )


@router.get(
    "/entreprises-counts",
    response_model=List[dict],
)
async def entreprises_counts(
    db: DBSession, user: CurrentUser
) -> List[dict]:
    """Pour chaque entreprise active du portefeuille, retourne le nombre
    d'immeubles qu'elle détient (via ImmeubleOwnership). Permet à l'UI
    de signaler les entreprises sans immeubles dans le sélecteur."""
    _require_volet(user)
    rows = (
        await db.execute(
            select(
                Entreprise.id,
                func.count(ImmeubleOwnership.id),
            )
            .select_from(Entreprise)
            .outerjoin(
                ImmeubleOwnership,
                ImmeubleOwnership.entreprise_id == Entreprise.id,
            )
            .where(Entreprise.is_active.is_(True))
            .group_by(Entreprise.id)
        )
    ).all()
    return [
        {"entreprise_id": int(eid), "nb_immeubles": int(cnt)}
        for eid, cnt in rows
    ]


@router.get(
    "/par-entreprise/{entreprise_id}",
    response_model=EntrepriseImmobilierSummary,
)
async def entreprise_immobilier_summary(
    entreprise_id: int, db: DBSession, user: CurrentUser
) -> EntrepriseImmobilierSummary:
    """Vue immobilière consolidée pour une entreprise propriétaire."""
    _require_volet(user)
    ent = await db.get(Entreprise, entreprise_id)
    if ent is None:
        raise HTTPException(status_code=404, detail="Entreprise introuvable.")

    ownerships = (
        await db.execute(
            select(ImmeubleOwnership).where(
                ImmeubleOwnership.entreprise_id == entreprise_id
            )
        )
    ).scalars().all()

    items: List[EntrepriseImmobilierImmeubleItem] = []
    total_nb_actifs = 0
    total_nb_occ = 0
    total_revenu = 0.0
    total_valeur = 0.0
    total_balance = 0.0

    for o in ownerships:
        imm = await db.get(Immeuble, o.immeuble_id)
        if imm is None:
            continue
        pct = float(o.ownership_pct or 0)
        nb_a, nb_o, rev_part, val_part, bal_part = await _compute_part_metrics(
            db, imm, pct
        )
        items.append(
            EntrepriseImmobilierImmeubleItem(
                immeuble_id=imm.id,
                name=imm.name,
                address=imm.address,
                city=imm.city,
                cover_photo_url=imm.cover_photo_url,
                ownership_pct=pct,
                nb_logements_actifs=nb_a,
                nb_logements_occupes=nb_o,
                revenu_mensuel_part=rev_part,
                valeur_part=val_part,
                balance_hyp_part=bal_part,
            )
        )
        total_nb_actifs += nb_a
        total_nb_occ += nb_o
        total_revenu += rev_part
        total_valeur += val_part
        total_balance += bal_part

    taux = (total_nb_occ / total_nb_actifs) if total_nb_actifs > 0 else 0.0
    return EntrepriseImmobilierSummary(
        entreprise_id=entreprise_id,
        nb_immeubles=len(items),
        nb_logements_actifs=total_nb_actifs,
        nb_logements_occupes=total_nb_occ,
        taux_occupation=round(taux, 4),
        revenu_mensuel_part=round(total_revenu, 2),
        revenu_annuel_part=round(total_revenu * 12, 2),
        valeur_portefeuille_part=round(total_valeur, 2),
        balance_hypothecaire_part=round(total_balance, 2),
        equity_part=round(total_valeur - total_balance, 2),
        immeubles=items,
    )
