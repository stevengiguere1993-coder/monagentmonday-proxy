"""Sérialiseurs d'entités métier — fondation réutilisable.

Objectif : transformer un objet ORM en ``dict`` JSON exposant les
**champs métier clés** (titre lisible, statut, montant, client/entité
liée, dates…) plutôt qu'un simple identifiant. Utilisé pour enrichir
l'activité (``GET /activity/me``) et, à terme, d'autres surfaces API /
MCP qui veulent renvoyer du contexte exploitable sans round-trip.

Principes :
  - **Défensif avant tout.** Chaque champ est lu via ``getattr(obj,
    "champ", None)`` — un modèle qui évolue (colonne renommée /
    supprimée) ne doit JAMAIS faire planter la sérialisation. On ne
    suppose l'existence d'aucun attribut.
  - **Deux niveaux.** ``level="summary"`` = champs essentiels pour les
    listes (activité). ``level="full"`` = vue détaillée (prévue pour
    plus tard ; remplie partiellement ici). ``summary`` est l'important.
  - **Registre.** ``serialize_entity(entity_type, obj, level)`` route
    vers le bon sérialiseur via ``SERIALIZERS``. Un type inconnu retourne
    un fallback minimal (id + type) plutôt qu'une exception.
  - **JSON-safe.** Les ``datetime`` sont rendus en ISO 8601 (str), les
    ``Decimal`` en ``float``, pour rester directement sérialisables par
    FastAPI / json sans encodeur custom.

Aucune I/O ici : ces fonctions opèrent sur des objets DÉJÀ chargés. Elles
ne déclenchent pas de requête (on lit des colonnes simples, pas des
relations paresseuses) pour rester sûres dans un contexte async.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Optional


# ── Helpers JSON-safe & lecture défensive ──────────────────────────


def _get(obj: Any, *names: str) -> Any:
    """Premier attribut non-None parmi ``names`` (lecture défensive).

    Permet de couvrir des modèles aux noms de champ divergents (ex.
    ``title`` vs ``name``) sans brancher partout. Retourne None si aucun
    n'existe / tous None."""
    for name in names:
        val = getattr(obj, name, None)
        if val is not None:
            return val
    return None


def _iso(value: Any) -> Optional[str]:
    """``datetime``/``date`` → chaîne ISO 8601, sinon None. Ne lève jamais."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        try:
            return value.isoformat()
        except Exception:
            return None
    # Déjà une chaîne (ou autre) : on renvoie tel quel si c'est du texte.
    if isinstance(value, str):
        return value
    return None


def _num(value: Any) -> Optional[float]:
    """``Decimal``/``int``/``float`` → ``float`` JSON-safe, sinon None."""
    if value is None:
        return None
    if isinstance(value, bool):  # un bool n'est pas un montant
        return None
    if isinstance(value, Decimal):
        try:
            return float(value)
        except Exception:
            return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _str(value: Any) -> Optional[str]:
    """Coerce en chaîne non vide, sinon None. Ne lève jamais."""
    if value is None:
        return None
    try:
        s = str(value).strip()
    except Exception:
        return None
    return s or None


# ── Sérialiseurs par entité ────────────────────────────────────────
#
# Signature uniforme : ``serialize_<entity>(obj, level="summary") -> dict``.
# Le dict ``summary`` porte toujours au minimum : ``entity_type``, ``id``,
# ``label`` (titre lisible), ``pole`` quand c'est pertinent. ``full``
# ajoute le détail.


def serialize_devlog_soumission(obj: Any, level: str = "summary") -> dict:
    """Soumission (devis) du pôle Développement logiciel.

    Champs métier : titre (= « numéro » lisible), client/lead cible,
    statut, montant. La soumission n'a pas de colonne ``numero`` dédiée :
    le ``title`` fait office d'identifiant humain. Le client n'étant pas
    une relation chargée par défaut, on expose les ids cibles + le nom du
    client/lead s'il a été préchargé (``client``/``lead``)."""
    data: dict[str, Any] = {
        "entity_type": "devlog_soumission",
        "id": getattr(obj, "id", None),
        "label": _str(_get(obj, "title")),
        "title": _str(_get(obj, "title")),
        "pole": "devlog",
        "status": _str(_get(obj, "status")),
        # Montant total (TTC tel que stocké) — colonne ``amount``.
        "amount": _num(_get(obj, "amount")),
        # Cibles : on expose les ids (toujours dispo) + le nom si la
        # relation a été préchargée (sinon None, jamais d'I/O ici).
        "client_id": _get(obj, "client_id"),
        "lead_id": _get(obj, "lead_id"),
        "client_name": _client_name(obj),
    }
    if level == "full":
        data.update(
            {
                "is_devis_dev": bool(getattr(obj, "is_devis_dev", False)),
                "summary": _str(_get(obj, "summary")),
                "notes": _str(_get(obj, "notes")),
                "client_recurring_description": _str(
                    _get(obj, "client_recurring_description")
                ),
                # Cible détaillée (lead + client), best-effort sans I/O.
                "lead": _related_party(getattr(obj, "lead", None)),
                "client": _related_party(getattr(obj, "client", None)),
                # Montants détaillés (HT / TPS / TVQ / TTC). Le modèle ne
                # stocke qu'``amount`` (TTC client) : on dérive le détail
                # fiscal Québec (TPS 5 % + TVQ 9.975 %) à titre indicatif.
                "amounts": _devlog_amounts(obj),
                # Taux & paramètres « devis_dev ».
                "marge_recurrente_pct": _num(_get(obj, "marge_recurrente_pct")),
                "marge_initiale_pct": _num(_get(obj, "marge_initiale_pct")),
                "commission_closer_pct": _num(
                    _get(obj, "commission_closer_pct")
                ),
                "taux_dev_horaire": _num(_get(obj, "taux_dev_horaire")),
                "taux_manager_horaire": _num(_get(obj, "taux_manager_horaire")),
                "heures_manager": _num(_get(obj, "heures_manager")),
                # Modules + fonctionnalités + tâches du chargé de projet,
                # si les relations ont été préchargées (best-effort).
                "modules": _devlog_modules(obj),
                "items": _devlog_items(obj),
                # Lien public de signature (si un jeton existe).
                "public_url": _devlog_public_url(obj),
                "has_signed_pdf": getattr(obj, "signed_pdf_blob", None)
                is not None,
                # Pipeline d'envoi / signature.
                "pricing_kind": _str(_get(obj, "pricing_kind")),
                "sent_at": _iso(_get(obj, "sent_at")),
                "signed_at": _iso(_get(obj, "signed_at")),
                "signed_name": _str(_get(obj, "signed_name")),
                "created_at": _iso(_get(obj, "created_at")),
                "updated_at": _iso(_get(obj, "updated_at")),
            }
        )
    return _drop_none(data)


#: Constantes fiscales Québec pour dériver le détail TPS/TVQ d'un montant
#: TTC (le modèle ``DevlogSoumission`` ne stocke que le total ``amount``).
_TPS_RATE = 0.05
_TVQ_RATE = 0.09975


def _devlog_amounts(obj: Any) -> Optional[dict]:
    """Détail des montants d'une soumission devlog. Le modèle ne stocke
    qu'``amount`` (TTC client) ; on dérive HT/TPS/TVQ à titre indicatif
    (taxes Québec). Aucun arrondi métier garanti : champ ``derived`` à True
    pour signaler que c'est calculé, pas faisant foi. None si pas de montant."""
    ttc = _num(_get(obj, "amount"))
    if ttc is None:
        return None
    try:
        ht = ttc / (1.0 + _TPS_RATE + _TVQ_RATE)
        tps = ht * _TPS_RATE
        tvq = ht * _TVQ_RATE
        return {
            "ttc": round(ttc, 2),
            "ht": round(ht, 2),
            "tps": round(tps, 2),
            "tvq": round(tvq, 2),
            "tps_rate": _TPS_RATE,
            "tvq_rate": _TVQ_RATE,
            "currency": "CAD",
            "derived": True,
        }
    except Exception:
        return {"ttc": ttc, "currency": "CAD", "derived": False}


def _devlog_public_url(obj: Any) -> Optional[str]:
    """Chemin relatif de la page publique de signature, si un jeton existe.
    On ne connaît pas l'hôte ici : on renvoie le chemin canonique."""
    token = _str(_get(obj, "signature_token"))
    if not token:
        return None
    return f"/devlog/sign-soumission/{token}"


def _devlog_modules(obj: Any) -> Optional[list]:
    """Modules d'une soumission devis_dev, si la relation ``modules`` est
    préchargée (best-effort, aucune I/O). Chaque module liste son nom, sa
    sélection et, si ses items sont accessibles, ses fonctionnalités."""
    modules = getattr(obj, "modules", None)
    if not modules:
        return None
    out: list[dict] = []
    try:
        for m in modules:
            entry: dict[str, Any] = {
                "id": getattr(m, "id", None),
                "name": _str(_get(m, "name")),
                "position": _get(m, "position"),
                "selected": bool(getattr(m, "selected", True)),
                "description": _str(_get(m, "description")),
            }
            out.append({k: v for k, v in entry.items() if v is not None
                        or k in ("id", "name")})
    except Exception:
        return None
    return out or None


def _devlog_items(obj: Any) -> Optional[list]:
    """Lignes (fonctionnalités, tâches de chargé de projet, coûts) d'une
    soumission, si la relation ``items`` est préchargée. On expose le
    libellé, le type (``item_kind``), les heures et le total par item."""
    items = getattr(obj, "items", None)
    if not items:
        return None
    out: list[dict] = []
    try:
        for it in items:
            entry: dict[str, Any] = {
                "id": getattr(it, "id", None),
                "description": _str(_get(it, "description")),
                "item_kind": _str(_get(it, "item_kind")),
                "module_id": _get(it, "module_id"),
                "heures": _num(_get(it, "heures")),
                "quantity": _num(_get(it, "quantity")),
                "unit": _str(_get(it, "unit")),
                "total": _num(_get(it, "total")),
            }
            out.append({k: v for k, v in entry.items() if v is not None
                        or k in ("id",)})
    except Exception:
        return None
    return out or None


def _related_party(target: Any) -> Optional[dict]:
    """Représentation minimale d'un lead / client lié (best-effort, sans
    I/O) : id + nom + entreprise + courriel + téléphone quand dispo."""
    if target is None:
        return None
    out: dict[str, Any] = {"id": getattr(target, "id", None)}
    name = _get(target, "name", "full_name", "nom", "title")
    if name is not None:
        out["name"] = _str(name)
    for src, dst in (
        ("company", "company"),
        ("email", "email"),
        ("phone", "phone"),
        ("status", "status"),
    ):
        val = _get(target, src)
        if val is not None:
            out[dst] = _str(val)
    return out if len(out) > 1 else None


def _client_name(obj: Any) -> Optional[str]:
    """Nom du client/lead lié à une soumission, SI la relation est déjà
    chargée (best-effort, aucune I/O). On tente plusieurs attributs
    courants pour rester robuste aux variations de modèle."""
    for rel in ("client", "lead"):
        target = getattr(obj, rel, None)
        if target is not None:
            name = _get(target, "name", "full_name", "nom", "title", "email")
            if name is not None:
                return _str(name)
    return None


def _serialize_task_common(
    obj: Any,
    *,
    entity_type: str,
    pole: str,
    title_attrs: tuple[str, ...],
    status: Optional[str],
    is_completed: Optional[bool],
    assignee: Any,
    level: str,
) -> dict:
    """Tronc commun de sérialisation d'une tâche (les 5 modèles).

    Les modèles divergent sur le nom du champ titre (``title`` vs
    ``name``), la forme du statut (chaîne libre vs booléen ``done``) et
    l'assignation (user_id vs employe_id). L'appelant normalise ces
    différences et passe les valeurs déjà résolues."""
    data: dict[str, Any] = {
        "entity_type": entity_type,
        "id": getattr(obj, "id", None),
        "label": _str(_get(obj, *title_attrs)),
        "title": _str(_get(obj, *title_attrs)),
        "pole": pole,
        "status": _str(status),
        "is_completed": bool(is_completed) if is_completed is not None else None,
        "assignee": assignee,
        "due_date": _iso(_get(obj, "due_date")),
        "updated_at": _iso(_get(obj, "updated_at")),
    }
    if level == "full":
        data.update(
            {
                "description": _str(_get(obj, "description", "notes")),
                "priority": _str(_get(obj, "priority")),
                "departement": _str(_get(obj, "departement")),
                "created_at": _iso(_get(obj, "created_at")),
                "completed_at": _iso(_get(obj, "completed_at", "done_at")),
                # Identifiants des entités parentes (selon le modèle) —
                # toujours best-effort, None si la colonne n'existe pas.
                "project_id": _get(obj, "project_id"),
                "phase_id": _get(obj, "phase_id"),
                "entreprise_id": _get(obj, "entreprise_id"),
                "deal_id": _get(obj, "deal_id"),
                # Scoring ICE (tâches entreprise / prospection), si présent.
                "impact": _get(obj, "impact"),
                "confidence": _get(obj, "confidence"),
                "effort": _get(obj, "effort"),
                "recurrence": _str(_get(obj, "recurrence")),
                # Assignation par employé (pôles construction / sales).
                "assignee_employe_id": _get(obj, "assignee_id"),
            }
        )
    return _drop_none(data)


def serialize_devlog_project_task(obj: Any, level: str = "summary") -> dict:
    """Tâche d'un projet de développement (pôle Développement logiciel).

    Statut chaîne (``a_faire``/``en_cours``/``termine``) ; complétée =
    statut ``termine``. Assignée via ``assignee_user_id``."""
    status = _str(_get(obj, "status"))
    return _serialize_task_common(
        obj,
        entity_type="devlog_project_task",
        pole="devlog",
        title_attrs=("title", "name"),
        status=status,
        is_completed=(status == "termine"),
        assignee=_assignee_user(obj),
        level=level,
    )


def serialize_entreprise_tache(obj: Any, level: str = "summary") -> dict:
    """Tâche du pôle Gestion d'entreprises.

    Statut chaîne (vocabulaire kanban) ; complétée = statut ``done``.
    Assignée via ``assignee_user_id``."""
    status = _str(_get(obj, "status"))
    return _serialize_task_common(
        obj,
        entity_type="entreprise_tache",
        pole="entreprise",
        title_attrs=("title", "name"),
        status=status,
        is_completed=(status == "done"),
        assignee=_assignee_user(obj),
        level=level,
    )


def serialize_prospection_deal_task(obj: Any, level: str = "summary") -> dict:
    """Tâche attachée à un deal du Pipeline Prospection.

    Le titre est porté par ``name`` (pas ``title``). Statut chaîne ;
    complétée = statut ``done``. Assignée via ``assignee_user_id``."""
    status = _str(_get(obj, "status"))
    return _serialize_task_common(
        obj,
        entity_type="prospection_deal_task",
        pole="prospection",
        title_attrs=("name", "title"),
        status=status,
        is_completed=(status == "done"),
        assignee=_assignee_user(obj),
        level=level,
    )


def serialize_sales_task(obj: Any, level: str = "summary") -> dict:
    """Tâche du CRM (ventes) — pôle Prospection côté API.

    Cycle de vie booléen : ``done``/``done_at`` (pas de statut chaîne).
    On dérive un statut lisible. Assignée à des employés (M2M) — on ne
    déréférence pas la relation ici (best-effort, sans I/O)."""
    done = bool(getattr(obj, "done", False))
    return _serialize_task_common(
        obj,
        entity_type="sales_task",
        pole="prospection",
        title_attrs=("title", "name"),
        status="done" if done else "open",
        is_completed=done,
        assignee=None,
        level=level,
    )


def serialize_project_task(obj: Any, level: str = "summary") -> dict:
    """Tâche d'un chantier (pôle Construction).

    Cycle de vie booléen : ``done``/``done_at``. Assignée à un employé
    via ``assignee_id`` (id seulement ; le nom requiert une jointure
    qu'on ne fait pas ici)."""
    done = bool(getattr(obj, "done", False))
    data = _serialize_task_common(
        obj,
        entity_type="project_task",
        pole="construction",
        title_attrs=("title", "name"),
        status="done" if done else "open",
        is_completed=done,
        assignee=None,
        level=level,
    )
    # Id d'employé assigné (sans résoudre le nom — pas d'I/O).
    emp_id = _get(obj, "assignee_id")
    if emp_id is not None:
        data["assignee_employe_id"] = emp_id
    return data


def _assignee_user(obj: Any) -> Optional[dict]:
    """Représentation minimale de l'assigné quand c'est un ``User`` lié
    par ``assignee_user_id``. On expose l'id systématiquement ; le nom /
    courriel seulement si la relation ``assignee`` a été préchargée."""
    uid = _get(obj, "assignee_user_id")
    if uid is None:
        return None
    out: dict[str, Any] = {"user_id": uid}
    target = getattr(obj, "assignee", None)
    if target is not None:
        name = _get(target, "full_name", "name", "email")
        if name is not None:
            out["name"] = _str(name)
    return out


def serialize_prospection_deal(obj: Any, level: str = "summary") -> dict:
    """Deal du Pipeline Prospection (opportunité sur un immeuble).

    Le modèle est volontairement minimal : adresse (sert de label),
    priorité (= étape pipeline), position. Des valeurs « riches » (prix,
    nombre de logements) vivent dans la fiche d'analyse liée
    (``lead_analysis``) — on les expose si la relation est préchargée,
    de façon best-effort et sans I/O."""
    data: dict[str, Any] = {
        "entity_type": "prospection_deal",
        "id": getattr(obj, "id", None),
        "label": _str(_get(obj, "address")),
        "address": _str(_get(obj, "address")),
        "pole": "prospection",
        # Le pipeline n'a pas de colonne « stage » : la priorité joue ce
        # rôle d'étape (urgent → ... → termine / abandonne).
        "status": _str(_get(obj, "priority")),
        "priority": _str(_get(obj, "priority")),
    }
    # Valeurs clés issues de l'analyse liée, si préchargée.
    analysis = getattr(obj, "lead_analysis", None)
    if analysis is not None:
        price = _num(
            _get(analysis, "asking_price", "price", "prix", "purchase_price")
        )
        units = _get(analysis, "num_units", "nb_logements", "units", "nb_units")
        if price is not None:
            data["price"] = price
        if units is not None:
            data["units"] = units
    if level == "full":
        data.update(
            {
                "position": _get(obj, "position"),
                "drive_folder_url": _str(_get(obj, "drive_folder_url")),
                "lead_analysis_id": _get(obj, "lead_analysis_id"),
                "created_at": _iso(_get(obj, "created_at")),
                "updated_at": _iso(_get(obj, "updated_at")),
                # Données d'analyse financière clés, si la fiche liée est
                # disponible (relation ``lead_analysis``, lazy=selectin).
                "analysis": _deal_analysis(analysis),
            }
        )
    return _drop_none(data)


def _deal_analysis(analysis: Any) -> Optional[dict]:
    """Champs clés d'une fiche ``LeadAnalysis`` liée à un deal (best-effort,
    aucune I/O). On expose adresse complète, prix, logements, revenus/
    dépenses et les résultats financiers résumés s'ils existent."""
    if analysis is None:
        return None
    out: dict[str, Any] = {"id": getattr(analysis, "id", None)}
    # Texte / identité.
    for src in ("status", "address", "city", "postal_code", "province",
                "type_batiment", "courtier_nom", "courtier_contact",
                "best_refi_program"):
        val = _str(_get(analysis, src))
        if val is not None:
            out[src] = val
    # Numériques (montants / compteurs / années).
    for src in ("asking_price", "revenus_bruts", "taxes_municipales",
                "taxes_scolaires", "assurances", "energie",
                "depenses_autres", "evaluation_municipale",
                "travaux_estimes", "best_refi_amount", "mdf_preteur_b",
                "superficie_terrain", "superficie_batiment"):
        val = _num(_get(analysis, src))
        if val is not None:
            out[src] = val
    for src in ("nb_logements", "annee_construction", "nb_stationnements"):
        val = _get(analysis, src)
        if val is not None:
            out[src] = val
    return out if len(out) > 1 else None


#: Statuts kanban d'une analyse de lead considérés « actifs / en cours »
#: (la fiche est encore en cours d'analyse / de décision, pas encore
#: classée intéressante ni abandonnée). Aligné sur ``LeadAnalysisStatus``.
_LEAD_ANALYSIS_ACTIVE_STATUSES = ("a_analyser", "decision_en_attente")
_LEAD_ANALYSIS_STATUS_LABELS = {
    "a_analyser": "À analyser",
    "decision_en_attente": "Décision en attente",
    "interessant": "Intéressant",
    "abandonne": "Abandonné",
}


def _lead_analysis_is_active(obj: Any) -> bool:
    """Une analyse est « en cours » si elle n'est pas abandonnée ET pas
    encore convertie en deal / lead du pipeline officiel. Best-effort."""
    status = (_str(_get(obj, "status")) or "").lower()
    if status == "abandonne":
        return False
    if _get(obj, "converted_to_deal_id") is not None:
        return False
    if _get(obj, "converted_to_lead_id") is not None:
        return False
    return True


def serialize_lead_analysis(obj: Any, level: str = "summary") -> dict:
    """Analyse de lead (fiche d'analyse financière d'un immeuble) — pôle
    Prospection.

    ``summary`` : adresse (label), étape kanban + libellé FR, flag « en
    cours », et quelques chiffres clés (prix demandé, logements, mise de
    fonds prêteur B, montant de refinancement) pour situer la fiche en un
    coup d'œil. ``full`` : tout le détail financier de l'analyse (revenus,
    dépenses, paramètres refi, résultats), les liens de conversion et les
    dates. Lecture défensive — aucun champ supposé présent."""
    status = _str(_get(obj, "status"))
    is_active = _lead_analysis_is_active(obj)
    data: dict[str, Any] = {
        "entity_type": "lead_analysis",
        "id": getattr(obj, "id", None),
        "label": _str(_get(obj, "address")) or f"Analyse #{getattr(obj, 'id', '?')}",
        "address": _str(_get(obj, "address")),
        "city": _str(_get(obj, "city")),
        "pole": "prospection",
        # Étape kanban = statut de la fiche d'analyse.
        "status": status,
        "status_label": _LEAD_ANALYSIS_STATUS_LABELS.get(
            (status or "").lower()
        ),
        # « En cours d'analyse » : ni abandonnée ni convertie.
        "is_active": is_active,
        # Chiffres clés pour situer la fiche dès le résumé.
        "asking_price": _num(_get(obj, "asking_price")),
        "nb_logements": _get(obj, "nb_logements"),
        "mdf_preteur_b": _num(_get(obj, "mdf_preteur_b")),
        "best_refi_amount": _num(_get(obj, "best_refi_amount")),
    }
    if level == "full":
        data.update(
            {
                "postal_code": _str(_get(obj, "postal_code")),
                "province": _str(_get(obj, "province")),
                "type_batiment": _str(_get(obj, "type_batiment")),
                "annee_construction": _get(obj, "annee_construction"),
                "nb_stationnements": _get(obj, "nb_stationnements"),
                "superficie_terrain": _num(_get(obj, "superficie_terrain")),
                "superficie_batiment": _num(_get(obj, "superficie_batiment")),
                "evaluation_municipale": _num(
                    _get(obj, "evaluation_municipale")
                ),
                "description": _str(_get(obj, "description")),
                "courtier_nom": _str(_get(obj, "courtier_nom")),
                "courtier_contact": _str(_get(obj, "courtier_contact")),
                # Revenus & dépenses (analyse financière).
                "revenus_bruts": _num(_get(obj, "revenus_bruts")),
                "taxes_municipales": _num(_get(obj, "taxes_municipales")),
                "taxes_scolaires": _num(_get(obj, "taxes_scolaires")),
                "assurances": _num(_get(obj, "assurances")),
                "energie": _num(_get(obj, "energie")),
                "depenses_autres": _num(_get(obj, "depenses_autres")),
                "travaux_estimes": _num(_get(obj, "travaux_estimes")),
                # Paramètres / résultats de refinancement (SCHL prêteur B).
                "best_refi_program": _str(_get(obj, "best_refi_program")),
                "mdf_preteur_b_pct": _num(_get(obj, "mdf_preteur_b_pct")),
                "tga_pct": _num(_get(obj, "tga_pct")),
                "taux_interet_achat_pct": _num(
                    _get(obj, "taux_interet_achat_pct")
                ),
                "taux_interet_refi_pct": _num(
                    _get(obj, "taux_interet_refi_pct")
                ),
                "duree_projet_annees": _get(obj, "duree_projet_annees"),
                # Liens de conversion vers le pipeline officiel.
                "converted_to_deal_id": _get(obj, "converted_to_deal_id"),
                "converted_to_lead_id": _get(obj, "converted_to_lead_id"),
                "model_used": _str(_get(obj, "model_used")),
                "notes": _str(_get(obj, "notes")),
                "created_at": _iso(_get(obj, "created_at")),
                "updated_at": _iso(_get(obj, "updated_at")),
            }
        )
    return _drop_none(data)


def serialize_devlog_project(obj: Any, level: str = "summary") -> dict:
    """Projet de développement (pôle Développement logiciel).

    Champs métier : nom (label), statut, échéance, client/soumission liés.
    Sert surtout la vue de LISTE des projets du pôle."""
    data: dict[str, Any] = {
        "entity_type": "devlog_project",
        "id": getattr(obj, "id", None),
        "label": _str(_get(obj, "name")),
        "name": _str(_get(obj, "name")),
        "pole": "devlog",
        "status": _str(_get(obj, "status")),
        "due_date": _iso(_get(obj, "due_date")),
        "client_id": _get(obj, "client_id"),
    }
    if level == "full":
        data.update(
            {
                "description": _str(_get(obj, "description")),
                "soumission_id": _get(obj, "soumission_id"),
                "start_date": _iso(_get(obj, "start_date")),
                "started_at": _iso(_get(obj, "started_at")),
                "delivered_at": _iso(_get(obj, "delivered_at")),
                "created_at": _iso(_get(obj, "created_at")),
                "updated_at": _iso(_get(obj, "updated_at")),
            }
        )
    return _drop_none(data)


def serialize_project(obj: Any, level: str = "summary") -> dict:
    """Projet / chantier (pôle Construction).

    Champs métier : nom (label), adresse, statut, budget. Sert surtout la
    vue de LISTE des chantiers du pôle."""
    data: dict[str, Any] = {
        "entity_type": "project",
        "id": getattr(obj, "id", None),
        "label": _str(_get(obj, "name")),
        "name": _str(_get(obj, "name")),
        "pole": "construction",
        "status": _str(_get(obj, "status")),
        "address": _str(_get(obj, "address")),
    }
    if level == "full":
        data.update(
            {
                "description": _str(_get(obj, "description")),
                "notes": _str(_get(obj, "notes")),
                "budget": _num(_get(obj, "budget")),
                "client_id": _get(obj, "client_id"),
                "soumission_id": _get(obj, "soumission_id"),
                "start_date": _iso(_get(obj, "start_date")),
                "end_date": _iso(_get(obj, "end_date")),
                "created_at": _iso(_get(obj, "created_at")),
                "updated_at": _iso(_get(obj, "updated_at")),
            }
        )
    return _drop_none(data)


def serialize_entreprise(obj: Any, level: str = "summary") -> dict:
    """Entreprise (entité d'affaire du pôle Gestion d'entreprises).

    Champs métier : nom (label), type/catégorie, NEQ. Le « contact
    principal » n'est pas une colonne directe (les partenaires vivent
    dans ``EntreprisePartner``) — on l'expose si une relation
    ``partners`` est préchargée, best-effort."""
    data: dict[str, Any] = {
        "entity_type": "entreprise",
        "id": getattr(obj, "id", None),
        "label": _str(_get(obj, "name")),
        "name": _str(_get(obj, "name")),
        "pole": "entreprise",
        "type": _str(_get(obj, "type")),
        "neq": _str(_get(obj, "neq")),
    }
    contact = _primary_contact(obj)
    if contact is not None:
        data["primary_contact"] = contact
    if level == "full":
        data.update(
            {
                "description": _str(_get(obj, "description")),
                "is_active": bool(getattr(obj, "is_active", True)),
                "is_parent_company": bool(
                    getattr(obj, "is_parent_company", False)
                ),
                "color_accent": _str(_get(obj, "color_accent")),
                "position": _get(obj, "position"),
                "monday_board_id": _str(_get(obj, "monday_board_id")),
                "monday_board_name": _str(_get(obj, "monday_board_name")),
                "drive_folder_url": _str(_get(obj, "drive_folder_url")),
                # Liste complète des partenaires si ``partners`` préchargé.
                "partners": _entreprise_partners(obj),
                "created_at": _iso(_get(obj, "created_at")),
                "updated_at": _iso(_get(obj, "updated_at")),
            }
        )
    return _drop_none(data)


def _entreprise_partners(obj: Any) -> Optional[list]:
    """Liste des partenaires d'une entreprise si ``partners`` est préchargé
    (best-effort, aucune I/O) : nom, courriel, rôle, % d'ownership."""
    partners = getattr(obj, "partners", None)
    if not partners:
        return None
    out: list[dict] = []
    try:
        for p in partners:
            entry: dict[str, Any] = {
                "id": getattr(p, "id", None),
                "name": _str(_get(p, "partner_name", "full_name", "name")),
                "email": _str(_get(p, "partner_email", "email")),
                "role": _str(_get(p, "role")),
                "ownership_pct": _num(_get(p, "ownership_pct")),
                "user_id": _get(p, "user_id"),
            }
            out.append({k: v for k, v in entry.items() if v is not None
                        or k in ("id",)})
    except Exception:
        return None
    return out or None


def _primary_contact(obj: Any) -> Optional[dict]:
    """Contact principal d'une entreprise, SI ``partners`` est préchargé.
    On prend le premier partenaire nommé (best-effort, sans I/O)."""
    partners = getattr(obj, "partners", None)
    if not partners:
        return None
    try:
        first = partners[0]
    except (TypeError, IndexError):
        return None
    name = _get(first, "partner_name", "full_name", "name")
    email = _get(first, "partner_email", "email")
    if name is None and email is None:
        return None
    out: dict[str, Any] = {}
    if name is not None:
        out["name"] = _str(name)
    if email is not None:
        out["email"] = _str(email)
    role = _get(first, "role")
    if role is not None:
        out["role"] = _str(role)
    return out or None


# ── Registre + façade ──────────────────────────────────────────────


def serialize_imm_tal_dossier(obj: Any, level: str = "summary") -> dict:
    """Dossier TAL d'un bail (pôle Immobilier, 2026-09-09).

    Résumé : motif, statut, dates, bail/locataire/logement/immeuble.
    Le libellé combine motif + statut pour qu'un Claude voie d'un coup
    d'œil de quoi il s'agit."""
    motif = _str(_get(obj, "motif"))
    statut = _str(_get(obj, "statut"))
    data: dict[str, Any] = {
        "entity_type": "imm_tal_dossier",
        "id": getattr(obj, "id", None),
        "label": f"Dossier TAL {motif or ''} ({statut or ''})".strip(),
        "pole": "immobilier",
        "motif": motif,
        "statut": statut,
        "status": statut,
        "numero_dossier": _str(_get(obj, "numero_dossier")),
        "bail_id": _get(obj, "bail_id"),
        "locataire_id": _get(obj, "locataire_id"),
        "logement_id": _get(obj, "logement_id"),
        "immeuble_id": _get(obj, "immeuble_id"),
        "ouvert_le": _iso(_get(obj, "ouvert_le")),
        "audience_le": _iso(_get(obj, "audience_le")),
        "decision_le": _iso(_get(obj, "decision_le")),
    }
    if level == "full":
        data.update(
            {
                "notes": _str(_get(obj, "notes")),
                "created_by_email": _str(_get(obj, "created_by_email")),
                "created_at": _iso(_get(obj, "created_at")),
                "updated_at": _iso(_get(obj, "updated_at")),
            }
        )
    return _drop_none(data)


def serialize_imm_locataire_contact(obj: Any, level: str = "summary") -> dict:
    """Garant / colocataire / occupant / contact d'urgence d'un
    locataire (pôle Immobilier, 2026-09-09)."""
    data: dict[str, Any] = {
        "entity_type": "imm_locataire_contact",
        "id": getattr(obj, "id", None),
        "label": _str(_get(obj, "full_name")),
        "full_name": _str(_get(obj, "full_name")),
        "pole": "immobilier",
        "role": _str(_get(obj, "role")),
        "locataire_id": _get(obj, "locataire_id"),
        "relation": _str(_get(obj, "relation")),
        "paie_le_loyer": bool(_get(obj, "paie_le_loyer")),
        "actif": bool(_get(obj, "actif")),
    }
    if level == "full":
        data.update(
            {
                "email": _str(_get(obj, "email")),
                "phone": _str(_get(obj, "phone")),
                "notes": _str(_get(obj, "notes")),
                "created_by_email": _str(_get(obj, "created_by_email")),
                "created_at": _iso(_get(obj, "created_at")),
                "updated_at": _iso(_get(obj, "updated_at")),
            }
        )
    return _drop_none(data)


def _drop_none(data: dict) -> dict:
    """Retire les clés à valeur None pour alléger la charge utile, tout
    en GARDANT toujours ``entity_type`` et ``id`` (identité de l'objet)."""
    keep = {"entity_type", "id"}
    return {k: v for k, v in data.items() if v is not None or k in keep}


#: Registre ``entity_type`` → fonction de sérialisation. Les clés
#: correspondent aux ``entity_type`` déjà utilisés par l'endpoint
#: d'activité (``devlog_project_task``, ``entreprise_tache``, …) pour un
#: branchement direct, plus les entités métier de plus haut niveau.
SERIALIZERS: dict[str, Callable[..., dict]] = {
    "devlog_soumission": serialize_devlog_soumission,
    "devlog_project_task": serialize_devlog_project_task,
    "entreprise_tache": serialize_entreprise_tache,
    "prospection_deal_task": serialize_prospection_deal_task,
    "sales_task": serialize_sales_task,
    "project_task": serialize_project_task,
    "prospection_deal": serialize_prospection_deal,
    "lead_analysis": serialize_lead_analysis,
    "devlog_project": serialize_devlog_project,
    "project": serialize_project,
    "entreprise": serialize_entreprise,
    "imm_tal_dossier": serialize_imm_tal_dossier,
    "imm_locataire_contact": serialize_imm_locataire_contact,
}


def serialize_entity(
    entity_type: str,
    obj: Any,
    level: str = "summary",
) -> dict:
    """Façade : sérialise ``obj`` selon son ``entity_type`` via le
    registre. Type inconnu / objet None → fallback minimal (jamais une
    exception). C'est le point d'entrée recommandé pour les appelants
    qui veulent enrichir un item d'activité.

    Ne lève jamais : un sérialiseur qui planterait (modèle inattendu)
    retombe sur le fallback identité, pour ne JAMAIS casser la réponse
    d'activité dont c'est un simple enrichissement."""
    if obj is None:
        return {"entity_type": entity_type, "id": None}
    fn = SERIALIZERS.get(entity_type)
    if fn is None:
        return {"entity_type": entity_type, "id": getattr(obj, "id", None)}
    try:
        return fn(obj, level=level)
    except Exception:
        # Enrichissement best-effort : on ne casse jamais l'appelant.
        return {"entity_type": entity_type, "id": getattr(obj, "id", None)}