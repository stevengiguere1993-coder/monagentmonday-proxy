"""Activité du compte (auth par clé d'API) + écriture de tâches par pôle.

  GET   /api/v1/activity/me?date=YYYY-MM-DD
  GET   /api/v1/activity/me?from=YYYY-MM-DD&to=YYYY-MM-DD
  GET   /api/v1/activity/me/summary?date=...
  GET   /api/v1/activity/members          (membres assignables)
  POST  /api/v1/activity/tasks            (créer une tâche d'un pôle)
  PATCH /api/v1/activity/tasks/{type}/{id}        (modifier une tâche)
  POST  /api/v1/activity/tasks/{type}/{id}/move   (déplacer une tâche)

LECTURE : retourne l'activité de l'utilisateur PROPRIÉTAIRE DE LA CLÉ sur
une période (par défaut : aujourd'hui, fuseau America/Toronto). Scope
strict : on ne voit jamais l'activité d'un autre utilisateur. L'activité
n'est renvoyée QUE pour les pôles dont la clé porte ``<pole>:activity:read``
(rétrocompat : clé sans scopes = tous les pôles).

ÉCRITURE : ``POST /activity/tasks`` crée une tâche dans un pôle donné, à
condition que la clé porte ``<pole>:tasks:create``. La tâche peut être
assignée à N'IMPORTE QUEL membre de l'équipe (paramètre ``assignee`` ;
défaut = propriétaire de la clé). ``PATCH /activity/tasks/{type}/{id}``
modifie N'IMPORTE QUELLE tâche (``<pole>:tasks:update``) ; ``POST
.../move`` la déplace d'une colonne kanban à l'autre (``<pole>:tasks:move``).
Tout est audité (mention « via clé API »).

Agrégé en lecture :
  - tasks  : tâches complétées / créées / modifiées (pôles autorisés).
  - audit  : entrées du journal d'audit où user_id = l'utilisateur.
  - summary: résumé en langage naturel (français) prêt à lire.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls, datetime, time, timedelta, timezone
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.api_key_deps import ApiKeyContext
from app.api.deps import DBSession
from app.models.audit_log import AuditLog
from app.models.devlog_project_task import DevlogProjectTask
from app.models.devlog_project import DevlogProject
from app.models.employe import Employe
from app.models.entreprise import Entreprise, EntreprisePartner
from app.models.entreprise_tache import EntrepriseTache
from app.models.immobilier import ImmLocataireContact, ImmTalDossier
from app.models.project import Project
from app.models.project_task import ProjectTask
from app.models.prospection_deal import ProspectionDeal
from app.models.prospection_deal_task import ProspectionDealTask
from app.models.sales_task import SalesTask, sales_task_assignees
from app.models.user import User
from app.services.api_capabilities import POLE_LABELS, readable_poles
from app.services.audit import log_action
from app.services.entity_serializers import serialize_entity

# Modèles supplémentaires chargés UNIQUEMENT pour la lecture détaillée
# (endpoints /activity/entities/...). Importés en tête : ces imports sont
# déjà tirés ailleurs dans l'app, donc sûrs au démarrage.
from app.models.devlog_soumission import DevlogSoumission
from app.models.devlog_soumission_module import DevlogSoumissionModule
from app.models.devlog_soumission_item import DevlogSoumissionItem
from app.models.devlog_client import DevlogClient
from app.models.devlog_lead import DevlogLead
from app.models.lead_analysis import LeadAnalysis


TORONTO = ZoneInfo("America/Toronto")

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/activity", tags=["activity"])


# Mapping « pôle interne du collecteur » → slug du catalogue de capacités.
# Les libellés internes (devlog/entreprise/prospection/sales/project) sont
# conservés dans la réponse pour ne rien casser, mais le FILTRAGE de
# lecture se fait sur les slugs du catalogue (prospection/devlog/
# construction/entreprise/...). Sales (CRM/prospects) → prospection ;
# Project (chantier) → construction.
_INTERNAL_POLE_TO_SLUG = {
    "devlog": "devlog",
    "entreprise": "entreprise",
    "prospection": "prospection",
    "sales": "prospection",
    "project": "construction",
}

# Préfixes d'audit (entity_type / action) → slug de pôle, pour filtrer le
# journal d'audit par pôle quand c'est possible.
_AUDIT_ENTITY_TO_SLUG = {
    "soumission": "devlog",
    "facture": "devlog",
    "devlog": "devlog",
    "entreprise": "entreprise",
    "prospection": "prospection",
    "deal": "prospection",
    "lead": "prospection",
    "client": "prospection",
    "project": "construction",
    "chantier": "construction",
    "achat": "comptabilite",
    "bon": "comptabilite",
    "fournisseur": "comptabilite",
    "immeuble": "immobilier",
    "imm": "immobilier",
    "bail": "immobilier",
}


def _audit_slug(entry: AuditLog) -> Optional[str]:
    """Devine le slug de pôle d'une entrée d'audit (best-effort), ou None
    si indéterminable. On regarde l'entity_type puis l'action."""
    haystacks = [entry.entity_type or "", entry.action or ""]
    for h in haystacks:
        low = h.lower()
        for key, slug in _AUDIT_ENTITY_TO_SLUG.items():
            if low.startswith(key) or f"{key}." in low or f"{key}_" in low:
                return slug
    return None


# ── Schémas ────────────────────────────────────────────────────────


class TaskActivity(BaseModel):
    pole: str                       # devlog / entreprise / prospection / sales / project
    entity_type: str                # type logique (pour reconstruire un lien)
    entity_id: int                  # id de la tâche
    title: str
    status: str                     # statut brut du pôle
    is_completed: bool
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Pourquoi la tâche apparaît dans la période : completed / created / updated.
    reasons: List[str]
    # Objet métier enrichi (niveau « summary ») de la tâche : titre lisible,
    # statut, assigné, pôle, échéance… Ajouté SANS remplacer les champs
    # ci-dessus (rétrocompat consommateurs). Best-effort : None si la
    # sérialisation échoue. Voir app.services.entity_serializers.
    entity: Optional[dict] = None


class AuditActivity(BaseModel):
    id: int
    action: str
    entity_type: Optional[str]
    entity_id: Optional[int]
    timestamp: datetime
    summary: str


class ActivityResponse(BaseModel):
    user_id: int
    user_email: str
    timezone: str
    period_start: datetime
    period_end: datetime
    tasks: List[TaskActivity]
    audit: List[AuditActivity]
    summary: str


class SummaryResponse(BaseModel):
    user_id: int
    period_start: datetime
    period_end: datetime
    summary: str


class TaskCreate(BaseModel):
    """Création d'une tâche via clé d'API. ``pole`` détermine le modèle
    cible et l'identifiant parent requis."""

    pole: str = Field(..., description="Slug du pôle : prospection / devlog / construction / entreprise.")
    # Identifiant de l'entité parente, selon le pôle :
    #   devlog        → parent_id = devlog_projects.id
    #   entreprise    → parent_id = entreprises.id
    #   prospection   → parent_id = prospection_deals.id
    #   construction  → parent_id = projects.id
    parent_id: int = Field(..., description="ID de l'entité parente (projet / entreprise / deal selon le pôle).")
    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = None
    due_date: Optional[date_cls] = None
    # Assigné OPTIONNEL : courriel, nom affiché ou id d'un membre QUELCONQUE
    # de l'équipe (pas seulement le propriétaire de la clé). Défaut = le
    # propriétaire de la clé (rétrocompat). Pour le pôle construction, le
    # membre est résolu sur une fiche Employe ; sinon sur un User.
    assignee: Optional[str] = Field(
        default=None,
        description=(
            "Membre à qui assigner la tâche : courriel, nom affiché ou id. "
            "Défaut = propriétaire de la clé."
        ),
    )


class TaskCreated(BaseModel):
    pole: str
    entity_type: str
    entity_id: int
    title: str
    status: str


class TaskUpdate(BaseModel):
    """Modification PARTIELLE d'une tâche (tous champs optionnels). Seuls
    les champs FOURNIS (non absents) sont appliqués."""

    title: Optional[str] = Field(default=None, max_length=500)
    description: Optional[str] = None
    status: Optional[str] = Field(
        default=None,
        description="Nouveau statut / colonne kanban (vocabulaire du pôle).",
    )
    priority: Optional[str] = None
    due_date: Optional[date_cls] = None
    # Assigné : membre quelconque (courriel / nom / id). Chaîne vide ou
    # « null » explicite → on désassigne.
    assignee: Optional[str] = Field(
        default=None,
        description=(
            "Nouvel assigné : courriel, nom affiché ou id d'un membre "
            "quelconque. Chaîne vide pour désassigner."
        ),
    )

    model_config = {"extra": "ignore"}


class TaskMove(BaseModel):
    """Déplacement d'une tâche : nouveau statut/colonne (+ position)."""

    status: str = Field(
        ..., description="Statut / colonne cible (vocabulaire du pôle)."
    )
    position: Optional[int] = Field(
        default=None,
        description="Position dans la colonne (si le modèle la supporte).",
    )


class TaskWriteResult(BaseModel):
    """Résultat d'une modification / déplacement de tâche."""

    pole: str
    entity_type: str
    entity_id: int
    title: Optional[str] = None
    status: str
    entity: Optional[dict] = None


class MemberOut(BaseModel):
    """Membre assignable (pour aider l'IA à choisir un assigné)."""

    kind: str                 # "user" ou "employe"
    id: int
    name: str
    email: Optional[str] = None


# ── Fenêtre temporelle (America/Toronto) ───────────────────────────


def _resolve_window(
    date: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> tuple[datetime, datetime]:
    """Retourne (start, end) en datetimes timezone-aware America/Toronto.

    - Si from/to fournis : du minuit de `from` au minuit (exclu) du
      lendemain de `to`.
    - Sinon `date` (ou aujourd'hui) : journée minuit→minuit (exclu).
    """
    def _parse(d: str) -> date_cls:
        try:
            return date_cls.fromisoformat(d)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Date invalide : « {d} » (format attendu YYYY-MM-DD).",
            )

    if date_from or date_to:
        start_day = _parse(date_from) if date_from else _parse(date_to)
        end_day = _parse(date_to) if date_to else _parse(date_from)
        if end_day < start_day:
            start_day, end_day = end_day, start_day
    else:
        target = _parse(date) if date else datetime.now(TORONTO).date()
        start_day = target
        end_day = target

    start = datetime.combine(start_day, time.min, tzinfo=TORONTO)
    # Fin exclusive = minuit du lendemain du dernier jour.
    end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=TORONTO)
    return start, end


def _in_window(dt: Optional[datetime], start: datetime, end: datetime) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TORONTO)
    return start <= dt < end


# ── Collecte des tâches par pôle ───────────────────────────────────


async def _employe_ids_for_user(db, user: User) -> List[int]:
    """Les pôles `sales` et `project` assignent via `employe_id`, pas via
    user_id. On relie l'utilisateur à ses fiches Employe par courriel."""
    if not user.email:
        return []
    stmt = select(Employe.id).where(Employe.email.ilike(user.email))
    return [row for row in (await db.execute(stmt)).scalars().all()]


def _reasons(
    is_completed: bool,
    completed_at: Optional[datetime],
    created_at: Optional[datetime],
    updated_at: Optional[datetime],
    start: datetime,
    end: datetime,
) -> List[str]:
    reasons: List[str] = []
    if is_completed and _in_window(completed_at, start, end):
        reasons.append("completed")
    if _in_window(created_at, start, end):
        reasons.append("created")
    if _in_window(updated_at, start, end) and "created" not in reasons:
        reasons.append("updated")
    return reasons


async def _collect_tasks(
    db,
    user: User,
    start: datetime,
    end: datetime,
    allowed_poles: Optional[set[str]] = None,
) -> List[TaskActivity]:
    """Collecte les tâches de l'utilisateur sur la période. Si
    ``allowed_poles`` est fourni (set de slugs du catalogue), on ne
    collecte QUE les modèles dont le slug est autorisé. None = tous
    (rétrocompat / usage interne sans filtrage)."""
    out: List[TaskActivity] = []
    employe_ids = await _employe_ids_for_user(db, user)

    def _allowed(internal_pole: str) -> bool:
        if allowed_poles is None:
            return True
        return _INTERNAL_POLE_TO_SLUG.get(internal_pole) in allowed_poles

    # ── Devlog (assignee_user_id ; status « termine » ; pas de
    #    completed_at → on prend updated_at comme proxy de complétion). ──
    if _allowed("devlog"):
        rows = (
            await db.execute(
                select(DevlogProjectTask).where(
                    DevlogProjectTask.assignee_user_id == user.id
                )
            )
        ).scalars().all()
        for t in rows:
            is_done = t.status == "termine"
            completed_at = t.updated_at if is_done else None
            reasons = _reasons(
                is_done, completed_at, t.created_at, t.updated_at, start, end
            )
            if reasons:
                out.append(
                    TaskActivity(
                        pole="devlog",
                        entity_type="devlog_project_task",
                        entity_id=t.id,
                        entity=serialize_entity("devlog_project_task", t),
                        title=t.title,
                        status=t.status,
                        is_completed=is_done,
                        completed_at=completed_at,
                        created_at=t.created_at,
                        updated_at=t.updated_at,
                        reasons=reasons,
                    )
                )

    # ── Entreprise (assignee_user_id ; status « done » ; completed_at). ──
    if _allowed("entreprise"):
        rows = (
            await db.execute(
                select(EntrepriseTache).where(
                    EntrepriseTache.assignee_user_id == user.id
                )
            )
        ).scalars().all()
        for t in rows:
            is_done = t.status == "done"
            completed_at = t.completed_at or (t.updated_at if is_done else None)
            reasons = _reasons(
                is_done, completed_at, t.created_at, t.updated_at, start, end
            )
            if reasons:
                out.append(
                    TaskActivity(
                        pole="entreprise",
                        entity_type="entreprise_tache",
                        entity_id=t.id,
                        entity=serialize_entity("entreprise_tache", t),
                        title=t.title,
                        status=t.status,
                        is_completed=is_done,
                        completed_at=completed_at,
                        created_at=t.created_at,
                        updated_at=t.updated_at,
                        reasons=reasons,
                    )
                )

    # ── Prospection (assignee_user_id ; status « done » ; pas de
    #    completed_at → updated_at comme proxy). ──
    if _allowed("prospection"):
        rows = (
            await db.execute(
                select(ProspectionDealTask).where(
                    ProspectionDealTask.assignee_user_id == user.id
                )
            )
        ).scalars().all()
        for t in rows:
            is_done = t.status == "done"
            completed_at = t.updated_at if is_done else None
            reasons = _reasons(
                is_done, completed_at, t.created_at, t.updated_at, start, end
            )
            if reasons:
                out.append(
                    TaskActivity(
                        pole="prospection",
                        entity_type="prospection_deal_task",
                        entity_id=t.id,
                        entity=serialize_entity("prospection_deal_task", t),
                        title=t.name,
                        status=t.status,
                        is_completed=is_done,
                        completed_at=completed_at,
                        created_at=t.created_at,
                        updated_at=t.updated_at,
                        reasons=reasons,
                    )
                )

    # ── Sales (assignation via employes ; done/done_at). → prospection ──
    if employe_ids and _allowed("sales"):
        rows = (
            await db.execute(
                select(SalesTask)
                .join(
                    sales_task_assignees,
                    sales_task_assignees.c.task_id == SalesTask.id,
                )
                .where(
                    sales_task_assignees.c.employe_id.in_(employe_ids)
                )
                .distinct()
            )
        ).scalars().all()
        for t in rows:
            completed_at = t.done_at or (t.updated_at if t.done else None)
            reasons = _reasons(
                t.done, completed_at, t.created_at, t.updated_at, start, end
            )
            if reasons:
                out.append(
                    TaskActivity(
                        pole="sales",
                        entity_type="sales_task",
                        entity_id=t.id,
                        entity=serialize_entity("sales_task", t),
                        title=t.title,
                        status="done" if t.done else "open",
                        is_completed=t.done,
                        completed_at=completed_at,
                        created_at=t.created_at,
                        updated_at=t.updated_at,
                        reasons=reasons,
                    )
                )

    # ── Project (assignee_id via employes ; done/done_at). → construction ──
    if employe_ids and _allowed("project"):
        rows = (
            await db.execute(
                select(ProjectTask).where(
                    ProjectTask.assignee_id.in_(employe_ids)
                )
            )
        ).scalars().all()
        for t in rows:
            completed_at = t.done_at or (t.updated_at if t.done else None)
            reasons = _reasons(
                t.done, completed_at, t.created_at, t.updated_at, start, end
            )
            if reasons:
                out.append(
                    TaskActivity(
                        pole="project",
                        entity_type="project_task",
                        entity_id=t.id,
                        entity=serialize_entity("project_task", t),
                        title=t.title,
                        status="done" if t.done else "open",
                        is_completed=t.done,
                        completed_at=completed_at,
                        created_at=t.created_at,
                        updated_at=t.updated_at,
                        reasons=reasons,
                    )
                )

    # Tri : les plus récents d'abord (par date d'événement la plus parlante).
    def _sort_key(ta: TaskActivity) -> datetime:
        return ta.completed_at or ta.updated_at or ta.created_at or start

    out.sort(key=_sort_key, reverse=True)
    return out


# ── Collecte de l'audit ────────────────────────────────────────────


def _humanize_audit(entry: AuditLog) -> str:
    """Résumé lisible d'une entrée d'audit (français), best-effort."""
    label_map = {
        "soumission.sent": "Soumission envoyée",
        "soumission.created": "Soumission créée",
        "facture.sent": "Facture envoyée",
        "facture.created": "Facture créée",
        "facture.paid": "Facture payée",
        "client.deleted": "Client supprimé",
        "api_key.created": "Clé d'API créée",
        "api_key.revoked": "Clé d'API révoquée",
    }
    base = label_map.get(entry.action)
    if base is None:
        # Repli générique « entité.verbe » → « Verbe sur entité #id ».
        base = entry.action.replace(".", " ").replace("_", " ").capitalize()
    if entry.entity_type and entry.entity_id is not None:
        return f"{base} ({entry.entity_type} #{entry.entity_id})"
    return base


async def _collect_audit(
    db,
    user: User,
    start: datetime,
    end: datetime,
    allowed_poles: Optional[set[str]] = None,
) -> List[AuditActivity]:
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.user_id == user.id,
            AuditLog.created_at >= start,
            AuditLog.created_at < end,
        )
        .order_by(AuditLog.created_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    out: List[AuditActivity] = []
    for e in rows:
        if allowed_poles is not None:
            slug = _audit_slug(e)
            # Si on devine le pôle et qu'il n'est pas autorisé → on cache.
            # Si on ne devine pas (slug None), on inclut (best-effort) tant
            # qu'au moins un pôle est en lecture (allowed_poles non vide).
            if slug is not None and slug not in allowed_poles:
                continue
            if slug is None and not allowed_poles:
                continue
        out.append(
            AuditActivity(
                id=e.id,
                action=e.action,
                entity_type=e.entity_type,
                entity_id=e.entity_id,
                timestamp=e.created_at,
                summary=_humanize_audit(e),
            )
        )
    return out


# ── Résumé en langage naturel ──────────────────────────────────────

_POLE_LABELS = {
    "devlog": "devlog",
    "entreprise": "entreprise",
    "prospection": "prospection",
    "sales": "ventes",
    "project": "chantier",
}


def _build_summary(
    tasks: List[TaskActivity],
    audit: List[AuditActivity],
    start: datetime,
    end: datetime,
    single_day: bool,
) -> str:
    if single_day:
        prefix = f"Le {start.date().isoformat()}"
    else:
        last_day = (end - timedelta(days=1)).date().isoformat()
        prefix = f"Du {start.date().isoformat()} au {last_day}"

    if not tasks and not audit:
        return f"{prefix} : aucune activité enregistrée."

    parts: List[str] = []

    # Tâches complétées, par pôle.
    completed = [t for t in tasks if "completed" in t.reasons]
    if completed:
        by_pole: dict[str, int] = {}
        for t in completed:
            by_pole[t.pole] = by_pole.get(t.pole, 0) + 1
        detail = ", ".join(
            f"{n} {_POLE_LABELS.get(p, p)}" for p, n in by_pole.items()
        )
        n = len(completed)
        word = "tâche complétée" if n == 1 else "tâches complétées"
        parts.append(f"{n} {word} ({detail})")

    created = [t for t in tasks if "created" in t.reasons]
    if created:
        n = len(created)
        word = "tâche créée" if n == 1 else "tâches créées"
        parts.append(f"{n} {word}")

    updated = [
        t for t in tasks
        if "updated" in t.reasons
        and "completed" not in t.reasons
        and "created" not in t.reasons
    ]
    if updated:
        n = len(updated)
        word = "tâche modifiée" if n == 1 else "tâches modifiées"
        parts.append(f"{n} {word}")

    # Audit : on met en avant quelques actions parlantes.
    audit_counts: dict[str, int] = {}
    for a in audit:
        audit_counts[a.action] = audit_counts.get(a.action, 0) + 1
    audit_phrases = {
        "soumission.sent": ("soumission envoyée", "soumissions envoyées"),
        "facture.sent": ("facture envoyée", "factures envoyées"),
        "facture.created": ("facture créée", "factures créées"),
        "facture.paid": ("facture payée", "factures payées"),
    }
    for action, (singular, plural) in audit_phrases.items():
        c = audit_counts.get(action, 0)
        if c:
            parts.append(f"{c} {singular if c == 1 else plural}")

    if not parts:
        # Activité présente mais sans catégorie « vedette » (ex. audit
        # divers uniquement).
        n = len(audit)
        word = "action enregistrée" if n == 1 else "actions enregistrées"
        parts.append(f"{n} {word} au journal d'audit")

    return f"{prefix} : " + ", ".join(parts) + "."


# ── Résolution d'un membre assignable (User ou Employe) ────────────
#
# L'assigné fourni par l'appelant est une chaîne souple : un id numérique,
# un courriel exact, ou un nom affiché. On résout vers l'id de l'entité
# d'assignation propre au pôle (User pour devlog/entreprise/prospection,
# Employe pour construction). Une correspondance ambiguë (plusieurs membres
# au même nom) lève ValueError pour éviter d'assigner au mauvais membre.


def _norm(s: Optional[str]) -> str:
    return (s or "").strip()


async def _resolve_user(db, raw: str) -> User:
    """Résout un membre (User actif) à partir d'un id / courriel / nom.
    Lève ValueError si introuvable ou ambigu."""
    token = _norm(raw)
    if not token:
        raise ValueError("Assigné vide.")

    # 1) id numérique.
    if token.isdigit():
        u = await db.get(User, int(token))
        if u is None or not u.is_active:
            raise ValueError(f"Utilisateur #{token} introuvable ou inactif.")
        return u

    # 2) courriel exact (insensible à la casse).
    if "@" in token:
        u = (
            await db.execute(
                select(User).where(
                    User.email.ilike(token), User.is_active.is_(True)
                )
            )
        ).scalars().first()
        if u is None:
            raise ValueError(f"Aucun membre actif avec le courriel « {token} ».")
        return u

    # 3) nom affiché (prénom + nom). On charge les users actifs et compare
    #    sur display_name / prénom / nom (insensible à la casse).
    users = (
        await db.execute(select(User).where(User.is_active.is_(True)))
    ).scalars().all()
    low = token.lower()
    matches = [
        u for u in users
        if low == _norm(u.display_name).lower()
        or low == _norm(u.email).split("@", 1)[0].lower()
    ]
    if not matches:
        # Repli : correspondance partielle (contient).
        matches = [u for u in users if low in _norm(u.display_name).lower()]
    if not matches:
        raise ValueError(f"Aucun membre ne correspond à « {token} ».")
    if len(matches) > 1:
        raise ValueError(
            f"« {token} » correspond à plusieurs membres ; précise un "
            "courriel ou un id."
        )
    return matches[0]


async def _resolve_employe(db, raw: str) -> Employe:
    """Résout un membre (Employe actif) à partir d'un id / courriel / nom.
    Lève ValueError si introuvable ou ambigu."""
    token = _norm(raw)
    if not token:
        raise ValueError("Assigné vide.")

    if token.isdigit():
        e = await db.get(Employe, int(token))
        if e is None or not e.active:
            raise ValueError(f"Employé #{token} introuvable ou inactif.")
        return e

    if "@" in token:
        e = (
            await db.execute(
                select(Employe).where(
                    Employe.email.ilike(token), Employe.active.is_(True)
                )
            )
        ).scalars().first()
        if e is None:
            raise ValueError(f"Aucun employé actif avec le courriel « {token} ».")
        return e

    emps = (
        await db.execute(
            select(Employe).where(Employe.active.is_(True))
        )
    ).scalars().all()
    low = token.lower()
    matches = [e for e in emps if low == _norm(e.full_name).lower()]
    if not matches:
        matches = [e for e in emps if low in _norm(e.full_name).lower()]
    if not matches:
        raise ValueError(f"Aucun employé ne correspond à « {token} ».")
    if len(matches) > 1:
        raise ValueError(
            f"« {token} » correspond à plusieurs employés ; précise un "
            "courriel ou un id."
        )
    return matches[0]


async def list_members(db) -> List[MemberOut]:
    """Liste des membres assignables : Users actifs + Employés actifs. Sert
    à l'IA pour choisir un assigné (REST + MCP). Lecture seule."""
    out: List[MemberOut] = []
    users = (
        await db.execute(select(User).where(User.is_active.is_(True)))
    ).scalars().all()
    for u in users:
        out.append(
            MemberOut(
                kind="user",
                id=u.id,
                name=u.display_name,
                email=u.email,
            )
        )
    emps = (
        await db.execute(select(Employe).where(Employe.active.is_(True)))
    ).scalars().all()
    for e in emps:
        out.append(
            MemberOut(
                kind="employe",
                id=e.id,
                name=e.full_name,
                email=e.email,
            )
        )
    out.sort(key=lambda m: (m.kind, m.name.lower()))
    return out


# ── Création de tâche par pôle (réutilisé par l'endpoint + le MCP) ──


async def create_task_for_pole(
    db,
    user: User,
    *,
    pole: str,
    parent_id: int,
    title: str,
    description: Optional[str] = None,
    due_date: Optional[date_cls] = None,
    assignee: Optional[str] = None,
    via: str = "api_key",
) -> TaskCreated:
    """Crée une tâche dans le pôle ``pole`` (slug du catalogue), rattachée
    à ``parent_id``. Par défaut assignée à ``user`` (propriétaire de la
    clé) ; si ``assignee`` est fourni, la tâche est assignée à CE membre
    quelconque (résolu par courriel / nom / id). Audité.

    Lève ValueError (→ 4xx côté appelant) si le pôle ne supporte pas la
    création de tâche, si l'entité parente est introuvable, ou si l'assigné
    est introuvable / ambigu. L'appelant DOIT déjà avoir vérifié la
    capacité ``<pole>:tasks:create``."""
    title = (title or "").strip()
    if not title:
        raise ValueError("Le titre de la tâche est requis.")

    pole = (pole or "").strip().lower()
    assignee_raw = _norm(assignee)

    if pole == "devlog":
        parent = await db.get(DevlogProject, parent_id)
        if parent is None:
            raise ValueError(f"Projet devlog #{parent_id} introuvable.")
        if assignee_raw:
            assignee_user_id = (await _resolve_user(db, assignee_raw)).id
        else:
            assignee_user_id = user.id
        task = DevlogProjectTask(
            project_id=parent_id,
            title=title,
            description=description or None,
            assignee_user_id=assignee_user_id,
            status="a_faire",
            priority="moyenne",
            due_date=due_date,
        )
        db.add(task)
        await db.flush()
        entity_type, entity_id, st = "devlog_project_task", task.id, task.status

    elif pole == "entreprise":
        parent = await db.get(Entreprise, parent_id)
        if parent is None:
            raise ValueError(f"Entreprise #{parent_id} introuvable.")
        if assignee_raw:
            assignee_user_id = (await _resolve_user(db, assignee_raw)).id
        else:
            assignee_user_id = user.id
        task = EntrepriseTache(
            entreprise_id=parent_id,
            title=title,
            description=description or None,
            assignee_user_id=assignee_user_id,
            status="a_faire",
            due_date=due_date,
        )
        db.add(task)
        await db.flush()
        entity_type, entity_id, st = "entreprise_tache", task.id, task.status

    elif pole == "prospection":
        parent = await db.get(ProspectionDeal, parent_id)
        if parent is None:
            raise ValueError(f"Deal de prospection #{parent_id} introuvable.")
        if assignee_raw:
            assignee_user_id = (await _resolve_user(db, assignee_raw)).id
        else:
            assignee_user_id = user.id
        task = ProspectionDealTask(
            deal_id=parent_id,
            name=title,
            notes=description or None,
            assignee_user_id=assignee_user_id,
            status="a_faire",
            due_date=due_date,
        )
        db.add(task)
        await db.flush()
        entity_type, entity_id, st = "prospection_deal_task", task.id, task.status

    elif pole == "construction":
        parent = await db.get(Project, parent_id)
        if parent is None:
            raise ValueError(f"Projet (chantier) #{parent_id} introuvable.")
        # Le pôle Construction assigne via employe_id : on résout sur une
        # fiche Employe. Si un assigné est fourni, on l'utilise ; sinon on
        # relie l'utilisateur à sa propre fiche Employe (par courriel).
        if assignee_raw:
            assignee_emp_id = (await _resolve_employe(db, assignee_raw)).id
        else:
            emp_ids = await _employe_ids_for_user(db, user)
            assignee_emp_id = emp_ids[0] if emp_ids else None
        task = ProjectTask(
            project_id=parent_id,
            title=title,
            description=description or None,
            assignee_id=assignee_emp_id,
            due_date=due_date,
            done=False,
        )
        db.add(task)
        await db.flush()
        entity_type, entity_id, st = "project_task", task.id, "open"

    else:
        raise ValueError(
            f"Le pôle « {pole} » ne supporte pas la création de tâche par clé d'API."
        )

    await log_action(
        db,
        user=user,
        action="task.created",
        entity_type=entity_type,
        entity_id=entity_id,
        details={
            "pole": pole,
            "parent_id": parent_id,
            "title": title,
            "assignee": assignee_raw or None,
            "via": via,
        },
    )

    return TaskCreated(
        pole=pole,
        entity_type=entity_type,
        entity_id=entity_id,
        title=title,
        status=st,
    )


# ── Registre d'ÉCRITURE par type de tâche (update / move) ──────────
#
# Pour chaque type de tâche on déclare comment écrire ses champs, sans
# réinventer la cascade métier : modèle ORM, slug de pôle (gouverne le
# scope), attribut titre, mode de statut (chaîne libre vs booléen done),
# statuts valides (kanban) et tokens de statut « terminé ». On réutilise
# exactement les vocabulaires des modèles (cf. TASK_STATUSES de chacun).


#: Tokens (insensibles à la casse) interprétés comme « terminé » pour les
#: modèles à statut BOOLÉEN (done). Tout le reste = non terminé.
_DONE_TOKENS = {"done", "termine", "terminé", "complete", "completed", "fait", "true", "1"}
_NOTDONE_TOKENS = {"open", "todo", "a_faire", "ouvert", "non_fait", "false", "0", "reopen", "rouvrir"}


class _TaskWriteSpec:
    """Description d'écriture d'un type de tâche.

    - model        : classe ORM.
    - pole         : slug du pôle (scope).
    - entity_type  : clé de sérialisation (entity_serializers).
    - title_attr   : nom du champ titre.
    - status_mode  : "string" (champ status chaîne) ou "bool" (done/done_at).
    - statuses     : tuple des statuts valides (mode string), sinon ().
    - assignee     : "user" | "employe" | None (pas d'assignation supportée).
    - assignee_attr: nom de la colonne d'assignation (si assignee user/employe).
    - has_priority : True si un champ priority existe.
    - has_position : True si un champ position existe.
    - completed_at_attr : colonne timestamp de complétion (mode bool/string), ou None.
    """

    __slots__ = (
        "model", "pole", "entity_type", "title_attr", "status_mode",
        "statuses", "assignee", "assignee_attr", "has_priority",
        "has_position", "completed_at_attr",
    )

    def __init__(
        self, *, model, pole, entity_type, title_attr, status_mode,
        statuses, assignee, assignee_attr, has_priority, has_position,
        completed_at_attr,
    ):
        self.model = model
        self.pole = pole
        self.entity_type = entity_type
        self.title_attr = title_attr
        self.status_mode = status_mode
        self.statuses = statuses
        self.assignee = assignee
        self.assignee_attr = assignee_attr
        self.has_priority = has_priority
        self.has_position = has_position
        self.completed_at_attr = completed_at_attr


_TASK_WRITE_ENTITIES: dict[str, _TaskWriteSpec] = {
    "devlog_project_task": _TaskWriteSpec(
        model=DevlogProjectTask, pole="devlog",
        entity_type="devlog_project_task", title_attr="title",
        status_mode="string",
        statuses=("a_faire", "en_cours", "termine"),
        assignee="user", assignee_attr="assignee_user_id",
        has_priority=True, has_position=False, completed_at_attr=None,
    ),
    "entreprise_tache": _TaskWriteSpec(
        model=EntrepriseTache, pole="entreprise",
        entity_type="entreprise_tache", title_attr="title",
        status_mode="string",
        statuses=("backlog", "todo", "a_faire", "in_progress", "waiting", "done"),
        assignee="user", assignee_attr="assignee_user_id",
        has_priority=True, has_position=True, completed_at_attr="completed_at",
    ),
    "prospection_deal_task": _TaskWriteSpec(
        model=ProspectionDealTask, pole="prospection",
        entity_type="prospection_deal_task", title_attr="name",
        status_mode="string",
        statuses=("todo", "a_faire", "in_progress", "waiting", "done"),
        assignee="user", assignee_attr="assignee_user_id",
        has_priority=True, has_position=True, completed_at_attr=None,
    ),
    "project_task": _TaskWriteSpec(
        model=ProjectTask, pole="construction",
        entity_type="project_task", title_attr="title",
        status_mode="bool",
        statuses=(),
        assignee="employe", assignee_attr="assignee_id",
        has_priority=False, has_position=True, completed_at_attr="done_at",
    ),
    "sales_task": _TaskWriteSpec(
        model=SalesTask, pole="prospection",
        entity_type="sales_task", title_attr="title",
        status_mode="bool",
        statuses=(),
        assignee=None, assignee_attr=None,
        has_priority=False, has_position=False, completed_at_attr="done_at",
    ),
}


def _apply_status(spec: _TaskWriteSpec, obj: Any, new_status: str) -> str:
    """Applique un nouveau statut/colonne à ``obj`` selon son mode. Retourne
    le statut effectif (lisible). Lève ValueError si le statut est invalide
    pour un modèle à statut chaîne."""
    token = _norm(new_status)
    if not token:
        raise ValueError("Le statut cible est requis.")

    if spec.status_mode == "string":
        low = token.lower()
        if low not in spec.statuses:
            raise ValueError(
                f"Statut « {new_status} » invalide pour {spec.entity_type}. "
                f"Valeurs acceptées : {', '.join(spec.statuses)}."
            )
        obj.status = low
        # Timestamp de complétion si la colonne existe.
        if spec.completed_at_attr:
            if low == "done":
                if getattr(obj, spec.completed_at_attr, None) is None:
                    setattr(obj, spec.completed_at_attr, datetime.now(timezone.utc))
            else:
                setattr(obj, spec.completed_at_attr, None)
        return low

    # Mode booléen (done/done_at).
    low = token.lower()
    if low in _DONE_TOKENS:
        done = True
    elif low in _NOTDONE_TOKENS:
        done = False
    else:
        raise ValueError(
            f"Statut « {new_status} » invalide pour {spec.entity_type}. "
            "Valeurs acceptées : done / termine (ou open / a_faire)."
        )
    obj.done = done
    if spec.completed_at_attr:
        if done:
            if getattr(obj, spec.completed_at_attr, None) is None:
                setattr(obj, spec.completed_at_attr, datetime.now(timezone.utc))
        else:
            setattr(obj, spec.completed_at_attr, None)
    return "done" if done else "open"


def _effective_status(spec: _TaskWriteSpec, obj: Any) -> str:
    if spec.status_mode == "string":
        return _norm(getattr(obj, "status", None)) or "?"
    return "done" if getattr(obj, "done", False) else "open"


async def _resolve_assignee_id(db, spec: _TaskWriteSpec, raw: str) -> Optional[int]:
    """Résout l'id d'assignation selon le mode du pôle, ou None pour
    désassigner (chaîne vide / « null »)."""
    token = _norm(raw)
    if token == "" or token.lower() in ("null", "none", "aucun", "unassign", "désassigner", "desassigner"):
        return None
    if spec.assignee == "user":
        return (await _resolve_user(db, token)).id
    if spec.assignee == "employe":
        return (await _resolve_employe(db, token)).id
    raise ValueError(
        f"L'assignation n'est pas supportée pour {spec.entity_type}."
    )


async def update_task_for_type(
    db,
    user: User,
    *,
    entity_type: str,
    entity_id: int,
    fields: dict[str, Any],
    via: str = "api_key",
) -> TaskWriteResult:
    """Modifie une tâche (N'IMPORTE laquelle, pas seulement celles du
    propriétaire). ``fields`` ne contient QUE les champs fournis par
    l'appelant (titre, description, status, priority, due_date, assignee).
    Audité.

    Lève ValueError (type/champ invalide, assigné introuvable) ou
    LookupError (tâche introuvable). L'appelant DOIT avoir vérifié
    ``<pole>:tasks:update``."""
    spec = _TASK_WRITE_ENTITIES.get(entity_type)
    if spec is None:
        raise ValueError(f"Type de tâche inconnu : « {entity_type} ».")

    obj = await db.get(spec.model, entity_id)
    if obj is None:
        raise LookupError(f"{entity_type} #{entity_id} introuvable.")

    changed: list[str] = []

    if "title" in fields and fields["title"] is not None:
        new_title = _norm(fields["title"])
        if not new_title:
            raise ValueError("Le titre ne peut pas être vide.")
        setattr(obj, spec.title_attr, new_title)
        changed.append("title")

    if "description" in fields:
        val = fields["description"]
        # Les tâches prospection portent la description dans « notes ».
        desc_attr = "notes" if hasattr(obj, "notes") and not hasattr(obj, "description") else "description"
        if hasattr(obj, desc_attr):
            setattr(obj, desc_attr, (_norm(val) or None) if val is not None else None)
            changed.append("description")

    if "status" in fields and fields["status"] is not None:
        _apply_status(spec, obj, str(fields["status"]))
        changed.append("status")

    if "priority" in fields and fields["priority"] is not None:
        if spec.has_priority and hasattr(obj, "priority"):
            obj.priority = _norm(fields["priority"]) or obj.priority
            changed.append("priority")
        # Sinon (modèle sans priorité) : on ignore silencieusement.

    if "due_date" in fields:
        val = fields["due_date"]
        if hasattr(obj, "due_date"):
            obj.due_date = val  # date_cls ou None
            changed.append("due_date")

    if "assignee" in fields and fields["assignee"] is not None:
        if spec.assignee is None or not spec.assignee_attr:
            raise ValueError(
                f"L'assignation n'est pas supportée pour {entity_type}."
            )
        new_id = await _resolve_assignee_id(db, spec, str(fields["assignee"]))
        setattr(obj, spec.assignee_attr, new_id)
        changed.append("assignee")

    await db.flush()

    await log_action(
        db,
        user=user,
        action="task.updated",
        entity_type=spec.entity_type,
        entity_id=entity_id,
        details={"pole": spec.pole, "changed": changed, "via": via},
    )

    return TaskWriteResult(
        pole=spec.pole,
        entity_type=spec.entity_type,
        entity_id=entity_id,
        title=_norm(getattr(obj, spec.title_attr, None)) or None,
        status=_effective_status(spec, obj),
        entity=serialize_entity(spec.entity_type, obj, level="full"),
    )


async def move_task_for_type(
    db,
    user: User,
    *,
    entity_type: str,
    entity_id: int,
    new_status: str,
    position: Optional[int] = None,
    via: str = "api_key",
) -> TaskWriteResult:
    """Déplace une tâche : change son statut / colonne kanban (+ position
    si le modèle la supporte). Audité.

    Lève ValueError (type/statut invalide) ou LookupError (introuvable).
    L'appelant DOIT avoir vérifié ``<pole>:tasks:move``."""
    spec = _TASK_WRITE_ENTITIES.get(entity_type)
    if spec is None:
        raise ValueError(f"Type de tâche inconnu : « {entity_type} ».")

    obj = await db.get(spec.model, entity_id)
    if obj is None:
        raise LookupError(f"{entity_type} #{entity_id} introuvable.")

    effective = _apply_status(spec, obj, new_status)

    if position is not None and spec.has_position and hasattr(obj, "position"):
        try:
            obj.position = int(position)
        except (TypeError, ValueError):
            raise ValueError("`position` doit être un entier.")

    await db.flush()

    await log_action(
        db,
        user=user,
        action="task.moved",
        entity_type=spec.entity_type,
        entity_id=entity_id,
        details={
            "pole": spec.pole,
            "status": effective,
            "position": position,
            "via": via,
        },
    )

    return TaskWriteResult(
        pole=spec.pole,
        entity_type=spec.entity_type,
        entity_id=entity_id,
        title=_norm(getattr(obj, spec.title_attr, None)) or None,
        status=effective,
        entity=serialize_entity(spec.entity_type, obj, level="full"),
    )


# ── Endpoints ──────────────────────────────────────────────────────


@router.get(
    "/me",
    response_model=ActivityResponse,
    summary="Activité de mon compte (clé d'API, pôles autorisés)",
)
async def my_activity(
    ctx: ApiKeyContext,
    db: DBSession,
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD (défaut: aujourd'hui)"),
    date_from: Optional[str] = Query(default=None, alias="from"),
    date_to: Optional[str] = Query(default=None, alias="to"),
) -> ActivityResponse:
    user = ctx.user
    poles = readable_poles(ctx.scopes)
    start, end = _resolve_window(date, date_from, date_to)
    single_day = (end - start) <= timedelta(days=1)

    tasks = await _collect_tasks(db, user, start, end, allowed_poles=poles)
    audit = await _collect_audit(db, user, start, end, allowed_poles=poles)
    summary = _build_summary(tasks, audit, start, end, single_day)

    return ActivityResponse(
        user_id=user.id,
        user_email=user.email,
        timezone="America/Toronto",
        period_start=start,
        period_end=end,
        tasks=tasks,
        audit=audit,
        summary=summary,
    )


@router.get(
    "/me/summary",
    response_model=SummaryResponse,
    summary="Résumé texte de mon activité (clé d'API, pôles autorisés)",
)
async def my_activity_summary(
    ctx: ApiKeyContext,
    db: DBSession,
    date: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None, alias="from"),
    date_to: Optional[str] = Query(default=None, alias="to"),
) -> SummaryResponse:
    user = ctx.user
    poles = readable_poles(ctx.scopes)
    start, end = _resolve_window(date, date_from, date_to)
    single_day = (end - start) <= timedelta(days=1)

    tasks = await _collect_tasks(db, user, start, end, allowed_poles=poles)
    audit = await _collect_audit(db, user, start, end, allowed_poles=poles)
    summary = _build_summary(tasks, audit, start, end, single_day)

    return SummaryResponse(
        user_id=user.id,
        period_start=start,
        period_end=end,
        summary=summary,
    )


@router.get(
    "/members",
    response_model=List[MemberOut],
    summary="Membres assignables (clé d'API)",
)
async def get_members(
    ctx: ApiKeyContext,
    db: DBSession,
) -> List[MemberOut]:
    """Liste les membres assignables (Users + Employés actifs) pour aider à
    choisir un assigné lors d'une création / modification de tâche.

    Accessible à toute clé qui peut lire au moins un pôle (rétrocompat : clé
    sans scopes lit tous les pôles)."""
    if not readable_poles(ctx.scopes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cette clé d'API ne peut lire aucun pôle.",
        )
    return await list_members(db)


@router.post(
    "/tasks",
    response_model=TaskCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Créer une tâche dans un pôle (clé d'API, capacité requise)",
)
async def create_task(
    payload: TaskCreate,
    ctx: ApiKeyContext,
    db: DBSession,
) -> TaskCreated:
    """Crée une tâche dans le pôle ``payload.pole``. Requiert la capacité
    ``<pole>:tasks:create`` sur la clé d'API (sinon 403). La tâche peut être
    assignée à n'importe quel membre (``payload.assignee``) ; par défaut au
    propriétaire de la clé. Auditée.

    La capacité requise dépend du pôle (issu du corps de la requête), donc
    on ne peut pas l'exprimer comme dépendance statique : on vérifie
    explicitement ``ctx.has_scope`` sur le contexte déjà authentifié."""
    pole = (payload.pole or "").strip().lower()
    if pole not in POLE_LABELS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Pôle inconnu : « {payload.pole} ».",
        )

    required = f"{pole}:tasks:create"
    if not ctx.has_scope(required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Capacité « Créer une tâche » non activée pour le pôle "
                f"« {POLE_LABELS[pole]} » sur cette clé d'API."
            ),
        )

    try:
        return await create_task_for_pole(
            db,
            ctx.user,
            pole=pole,
            parent_id=payload.parent_id,
            title=payload.title,
            description=payload.description,
            due_date=payload.due_date,
            assignee=payload.assignee,
            via="api_key",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


def _require_task_scope(ctx, entity_type: str, action: str) -> _TaskWriteSpec:
    """Récupère le spec d'écriture d'un type de tâche et vérifie la capacité
    ``<pole>:tasks:<action>``. 404 si le type est inconnu, 403 si la
    capacité manque. Retourne le spec en cas de succès."""
    spec = _TASK_WRITE_ENTITIES.get(entity_type)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Type de tâche inconnu : « {entity_type} ».",
        )
    required = f"{spec.pole}:tasks:{action}"
    if not ctx.has_scope(required):
        label = "Modifier une tâche" if action == "update" else "Déplacer une tâche"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Capacité « {label} » non activée pour le pôle "
                f"« {POLE_LABELS.get(spec.pole, spec.pole)} » sur cette clé d'API."
            ),
        )
    return spec


@router.patch(
    "/tasks/{entity_type}/{entity_id}",
    response_model=TaskWriteResult,
    summary="Modifier une tâche (clé d'API, capacité requise)",
)
async def update_task(
    payload: TaskUpdate,
    ctx: ApiKeyContext,
    db: DBSession,
    entity_type: str = Path(
        ...,
        description=(
            "Type de tâche : devlog_project_task, entreprise_tache, "
            "prospection_deal_task, project_task, sales_task."
        ),
    ),
    entity_id: int = Path(..., description="Id de la tâche."),
) -> TaskWriteResult:
    """Modifie N'IMPORTE QUELLE tâche du type donné (pas seulement celles du
    propriétaire). Requiert ``<pole>:tasks:update``. Seuls les champs
    fournis sont appliqués."""
    _require_task_scope(ctx, entity_type, "update")
    # On ne transmet QUE les champs explicitement fournis (model_fields_set)
    # pour distinguer « absent » de « mis à None » (ex. due_date à effacer).
    fields = {k: getattr(payload, k) for k in payload.model_fields_set}
    if not fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Aucun champ à modifier n'a été fourni.",
        )
    try:
        return await update_task_for_type(
            db,
            ctx.user,
            entity_type=entity_type,
            entity_id=entity_id,
            fields=fields,
            via="api_key",
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )


@router.post(
    "/tasks/{entity_type}/{entity_id}/move",
    response_model=TaskWriteResult,
    summary="Déplacer une tâche (clé d'API, capacité requise)",
)
async def move_task(
    payload: TaskMove,
    ctx: ApiKeyContext,
    db: DBSession,
    entity_type: str = Path(..., description="Type de tâche."),
    entity_id: int = Path(..., description="Id de la tâche."),
) -> TaskWriteResult:
    """Déplace N'IMPORTE QUELLE tâche du type donné dans une nouvelle
    colonne / étape kanban (+ position si applicable). Requiert
    ``<pole>:tasks:move``."""
    _require_task_scope(ctx, entity_type, "move")
    try:
        return await move_task_for_type(
            db,
            ctx.user,
            entity_type=entity_type,
            entity_id=entity_id,
            new_status=payload.status,
            position=payload.position,
            via="api_key",
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )


# ── Lecture détail d'une entité (JSON « full ») ────────────────────
#
# Renvoie le JSON complet d'une entité métier par son id, en respectant
# le scope de pôle de la clé. Réutilisé par les endpoints REST ci-dessous
# ET par les outils MCP (kratos_get_*). Aucune écriture ; lecture seule.
#
# Chaque type d'entité déclare : son modèle ORM, le slug de pôle qui
# gouverne le scope, le type de sérialisation, et la capacité de lecture
# détail dédiée. L'autorisation est accordée si la clé porte la capacité
# détail OU peut lire l'activité de ce pôle (``<pole>:activity:read``) —
# ce qui préserve la RÉTROCOMPAT (clé sans scopes = lecture tous pôles).


#: Type d'entité de détail → (modèle ORM, slug de pôle, entity_type de
#: sérialisation, capacité de lecture détail dédiée).
_DETAIL_ENTITIES: dict[str, tuple] = {
    "soumission": (
        DevlogSoumission, "devlog", "devlog_soumission",
        "devlog:soumissions:read",
    ),
    "devlog_soumission": (
        DevlogSoumission, "devlog", "devlog_soumission",
        "devlog:soumissions:read",
    ),
    "deal": (
        ProspectionDeal, "prospection", "prospection_deal",
        "prospection:deals:read",
    ),
    "prospection_deal": (
        ProspectionDeal, "prospection", "prospection_deal",
        "prospection:deals:read",
    ),
    "entreprise": (
        Entreprise, "entreprise", "entreprise", "entreprise:read",
    ),
    "lead_analysis": (
        LeadAnalysis, "prospection", "lead_analysis",
        "prospection:analyses:read",
    ),
    # Tâches : chaque modèle a son slug de pôle et sa capacité <pole>:tasks:read.
    "devlog_project_task": (
        DevlogProjectTask, "devlog", "devlog_project_task",
        "devlog:tasks:read",
    ),
    "entreprise_tache": (
        EntrepriseTache, "entreprise", "entreprise_tache",
        "entreprise:tasks:read",
    ),
    "prospection_deal_task": (
        ProspectionDealTask, "prospection", "prospection_deal_task",
        "prospection:tasks:read",
    ),
    "sales_task": (
        SalesTask, "prospection", "sales_task", "prospection:tasks:read",
    ),
    "project_task": (
        ProjectTask, "construction", "project_task",
        "construction:tasks:read",
    ),
    # Immobilier (2026-09-09) : dossiers TAL + garants/contacts. Pas de
    # capacité dédiée dans le catalogue → lisibles via
    # ``immobilier:activity:read`` (clé sans scopes = tous les pôles).
    "tal_dossier": (
        ImmTalDossier, "immobilier", "imm_tal_dossier",
        "immobilier:tal_dossiers:read",
    ),
    "locataire_contact": (
        ImmLocataireContact, "immobilier", "imm_locataire_contact",
        "immobilier:locataire_contacts:read",
    ),
}


def _can_read_entity(ctx, pole: str, detail_cap: str) -> bool:
    """La clé peut-elle lire le détail d'une entité de ce pôle ?

    Vrai si elle porte la capacité de lecture détail dédiée OU si elle peut
    lire l'activité du pôle (``<pole>:activity:read``). Ce second test
    couvre la RÉTROCOMPAT : une clé sans scopes lit tous les pôles."""
    return ctx.has_scope(detail_cap) or ctx.has_scope(f"{pole}:activity:read")


async def _enrich_soumission(db, obj: DevlogSoumission) -> None:
    """Précharge (best-effort, sans casser) lead/client/modules/items sur
    une soumission devlog pour que le serializer « full » les expose. Les
    attributs sont posés en clair sur l'instance (pas des relations ORM)."""
    try:
        if getattr(obj, "lead_id", None) is not None:
            obj.lead = await db.get(DevlogLead, obj.lead_id)
        if getattr(obj, "client_id", None) is not None:
            obj.client = await db.get(DevlogClient, obj.client_id)
        mods = (
            await db.execute(
                select(DevlogSoumissionModule)
                .where(DevlogSoumissionModule.soumission_id == obj.id)
                .order_by(DevlogSoumissionModule.position)
            )
        ).scalars().all()
        obj.modules = list(mods)
        items = (
            await db.execute(
                select(DevlogSoumissionItem)
                .where(DevlogSoumissionItem.soumission_id == obj.id)
                .order_by(DevlogSoumissionItem.position)
            )
        ).scalars().all()
        obj.items = list(items)
    except Exception:
        # L'enrichissement est best-effort : le détail de base reste servi.
        pass


async def _enrich_entreprise(db, obj: Entreprise) -> None:
    """Précharge les partenaires d'une entreprise (best-effort)."""
    try:
        partners = (
            await db.execute(
                select(EntreprisePartner)
                .where(EntreprisePartner.entreprise_id == obj.id)
            )
        ).scalars().all()
        obj.partners = list(partners)
    except Exception:
        pass


async def load_entity_full(
    db,
    ctx,
    entity_type: str,
    entity_id: int,
) -> dict:
    """Charge une entité par son id et retourne son JSON « full » sérialisé,
    après vérification du scope de pôle. Réutilisé par REST et MCP.

    Lève :
      - ValueError("unknown") si ``entity_type`` n'est pas reconnu ;
      - PermissionError(message) si la clé n'a pas le scope requis ;
      - LookupError(message) si l'entité est introuvable.
    """
    spec = _DETAIL_ENTITIES.get(entity_type)
    if spec is None:
        raise ValueError("unknown")
    model, pole, ser_type, detail_cap = spec

    if not _can_read_entity(ctx, pole, detail_cap):
        raise PermissionError(
            f"Lecture du détail non autorisée pour le pôle "
            f"« {POLE_LABELS.get(pole, pole)} » sur cette clé d'API."
        )

    obj = await db.get(model, entity_id)
    if obj is None:
        raise LookupError(f"{entity_type} #{entity_id} introuvable.")

    # Préchargement spécifique selon le type (best-effort).
    if ser_type == "devlog_soumission":
        await _enrich_soumission(db, obj)
    elif ser_type == "entreprise":
        await _enrich_entreprise(db, obj)

    return serialize_entity(ser_type, obj, level="full")


@router.get(
    "/entities/{entity_type}/{entity_id}",
    summary="Lire le JSON détaillé d'une entité par son id (clé d'API)",
)
async def get_entity_detail(
    ctx: ApiKeyContext,
    db: DBSession,
    entity_type: str = Path(
        ...,
        description=(
            "Type d'entité : soumission, deal, entreprise, ou un type de "
            "tâche (devlog_project_task, entreprise_tache, "
            "prospection_deal_task, sales_task, project_task)."
        ),
    ),
    entity_id: int = Path(..., description="Identifiant de l'entité."),
) -> dict:
    """Renvoie le JSON « full » d'une entité métier (lecture seule),
    en respectant le scope de pôle de la clé.

    404 si le type est inconnu ou l'entité introuvable ; 403 si la clé n'a
    pas le scope de lecture du pôle correspondant."""
    try:
        return await load_entity_full(db, ctx, entity_type, entity_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Type d'entité inconnu : « {entity_type} ».",
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        )


@router.get(
    "/entities/soumission/{entity_id}",
    summary="Détail d'une soumission devlog (clé d'API)",
)
async def get_soumission_detail(
    ctx: ApiKeyContext, db: DBSession,
    entity_id: int = Path(..., description="Id de la soumission devlog."),
) -> dict:
    try:
        return await load_entity_full(db, ctx, "devlog_soumission", entity_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "/entities/deal/{entity_id}",
    summary="Détail d'un deal de prospection (clé d'API)",
)
async def get_deal_detail(
    ctx: ApiKeyContext, db: DBSession,
    entity_id: int = Path(..., description="Id du deal de prospection."),
) -> dict:
    try:
        return await load_entity_full(db, ctx, "prospection_deal", entity_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "/entities/entreprise/{entity_id}",
    summary="Détail d'une entreprise (clé d'API)",
)
async def get_entreprise_detail(
    ctx: ApiKeyContext, db: DBSession,
    entity_id: int = Path(..., description="Id de l'entreprise."),
) -> dict:
    try:
        return await load_entity_full(db, ctx, "entreprise", entity_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "/entities/lead_analysis/{entity_id}",
    summary="Détail d'une analyse de lead (clé d'API)",
)
async def get_lead_analysis_detail(
    ctx: ApiKeyContext, db: DBSession,
    entity_id: int = Path(..., description="Id de l'analyse de lead."),
) -> dict:
    """Renvoie le JSON « full » d'une analyse de lead (fiche d'analyse
    financière) du pôle Prospection. Respecte le scope de pôle."""
    try:
        return await load_entity_full(db, ctx, "lead_analysis", entity_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ── Lecture d'ENSEMBLE (liste) d'une entité d'un pôle ──────────────
#
# Renvoie une LISTE paginée (résumés) de toutes les entités d'un type,
# pour donner au connecteur une vue d'ensemble d'un pôle (deals du
# pipeline, analyses de leads en cours, soumissions, projets, entreprises)
# plutôt qu'une seule entité par id. Lecture seule, scope de pôle respecté
# (mêmes règles que la lecture détail : capacité de liste dédiée OU
# ``<pole>:activity:read`` → couvre la rétrocompat).
#
# Pagination simple : ``limit`` (défaut 50, plafond 100) + ``offset``. La
# réponse indique ``truncated=True`` quand d'autres résultats existent
# au-delà de la page renvoyée, pour ne pas faire croire à une liste
# complète. Filtre optionnel par étape / statut (``stage``), et filtre
# « actives uniquement » pour les analyses (``active_only``).

#: Limite par défaut et plafond d'une page de liste.
_LIST_DEFAULT_LIMIT = 50
_LIST_MAX_LIMIT = 100


class _ListSpec:
    """Description de la lecture en liste d'un type d'entité.

    - model       : classe ORM.
    - pole        : slug du pôle (gouverne le scope).
    - entity_type : clé de sérialisation (entity_serializers).
    - list_cap    : capacité de liste dédiée (<pole>:...:list).
    - order_attr  : colonne de tri (décroissant) — id si None.
    - stage_attr  : colonne filtrable par « étape / statut » (ou None).
    - supports_active : True si le type supporte le filtre « en cours »
                        (réservé aux analyses de leads).
    """

    __slots__ = (
        "model", "pole", "entity_type", "list_cap", "order_attr",
        "stage_attr", "supports_active",
    )

    def __init__(
        self, *, model, pole, entity_type, list_cap, order_attr,
        stage_attr, supports_active,
    ):
        self.model = model
        self.pole = pole
        self.entity_type = entity_type
        self.list_cap = list_cap
        self.order_attr = order_attr
        self.stage_attr = stage_attr
        self.supports_active = supports_active


#: Type de liste (clé d'URL / d'outil MCP) → spec. Plusieurs alias pointent
#: vers le même spec (ex. « deals » et « prospection_deal »).
_LIST_ENTITIES: dict[str, _ListSpec] = {
    "deals": _ListSpec(
        model=ProspectionDeal, pole="prospection",
        entity_type="prospection_deal", list_cap="prospection:deals:list",
        order_attr="position", stage_attr="priority", supports_active=False,
    ),
    "analyses": _ListSpec(
        model=LeadAnalysis, pole="prospection",
        entity_type="lead_analysis", list_cap="prospection:analyses:list",
        order_attr="updated_at", stage_attr="status", supports_active=True,
    ),
    "soumissions": _ListSpec(
        model=DevlogSoumission, pole="devlog",
        entity_type="devlog_soumission", list_cap="devlog:soumissions:list",
        order_attr="id", stage_attr="status", supports_active=False,
    ),
    "devlog_projects": _ListSpec(
        model=DevlogProject, pole="devlog",
        entity_type="devlog_project", list_cap="devlog:projects:list",
        order_attr="updated_at", stage_attr="status", supports_active=False,
    ),
    "entreprises": _ListSpec(
        model=Entreprise, pole="entreprise",
        entity_type="entreprise", list_cap="entreprise:list",
        order_attr="position", stage_attr=None, supports_active=False,
    ),
    "projects": _ListSpec(
        model=Project, pole="construction",
        entity_type="project", list_cap="construction:projects:list",
        order_attr="updated_at", stage_attr="status", supports_active=False,
    ),
    # Immobilier (2026-09-09) — `stage` filtre le statut du dossier TAL
    # (a_ouvrir | ouvert | audience | decision | ferme) ou le rôle du
    # contact (garant | colocataire | occupant | urgence).
    "tal_dossiers": _ListSpec(
        model=ImmTalDossier, pole="immobilier",
        entity_type="imm_tal_dossier",
        list_cap="immobilier:tal_dossiers:list",
        order_attr="updated_at", stage_attr="statut", supports_active=False,
    ),
    "locataire_contacts": _ListSpec(
        model=ImmLocataireContact, pole="immobilier",
        entity_type="imm_locataire_contact",
        list_cap="immobilier:locataire_contacts:list",
        order_attr="updated_at", stage_attr="role", supports_active=False,
    ),
}

#: Alias supplémentaires (noms « complets ») vers les mêmes specs, pour que
#: l'API accepte aussi bien « deals » que « prospection_deal ».
_LIST_ALIASES: dict[str, str] = {
    "prospection_deal": "deals",
    "prospection_deals": "deals",
    "lead_analysis": "analyses",
    "lead_analyses": "analyses",
    "devlog_soumission": "soumissions",
    "devlog_project": "devlog_projects",
    "entreprise": "entreprises",
    "project": "projects",
    "construction_projects": "projects",
    "tal_dossier": "tal_dossiers",
    "imm_tal_dossier": "tal_dossiers",
    "locataire_contact": "locataire_contacts",
    "imm_locataire_contact": "locataire_contacts",
    "garants": "locataire_contacts",
}


def _resolve_list_spec(list_type: str) -> Optional[_ListSpec]:
    key = (list_type or "").strip().lower()
    if key in _LIST_ENTITIES:
        return _LIST_ENTITIES[key]
    alias = _LIST_ALIASES.get(key)
    if alias is not None:
        return _LIST_ENTITIES.get(alias)
    return None


def _can_list_entity(ctx, spec: _ListSpec) -> bool:
    """La clé peut-elle lister ce type ? Capacité de liste dédiée OU lecture
    d'activité du pôle (couvre la rétrocompat clé sans scopes)."""
    return ctx.has_scope(spec.list_cap) or ctx.has_scope(
        f"{spec.pole}:activity:read"
    )


async def list_entities(
    db,
    ctx,
    list_type: str,
    *,
    stage: Optional[str] = None,
    active_only: bool = False,
    limit: int = _LIST_DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """Liste paginée (résumés) des entités d'un type, scope de pôle
    respecté. Réutilisé par REST et MCP.

    Lève :
      - ValueError("unknown") si ``list_type`` n'est pas reconnu ;
      - PermissionError(message) si la clé n'a pas le scope requis.

    Retourne ``{entity_type, pole, items, count, limit, offset, truncated}``.
    ``truncated`` est True s'il reste des résultats au-delà de la page.
    """
    spec = _resolve_list_spec(list_type)
    if spec is None:
        raise ValueError("unknown")

    if not _can_list_entity(ctx, spec):
        raise PermissionError(
            f"Lecture de liste non autorisée pour le pôle "
            f"« {POLE_LABELS.get(spec.pole, spec.pole)} » sur cette clé d'API."
        )

    # Borne la pagination (sécurité : on ne renvoie jamais > _LIST_MAX_LIMIT).
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _LIST_DEFAULT_LIMIT
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, _LIST_MAX_LIMIT))
    offset = max(0, offset)

    stmt = select(spec.model)

    # Filtre par étape / statut (sur la colonne déclarée).
    stage_token = _norm(stage)
    if stage_token and spec.stage_attr:
        col = getattr(spec.model, spec.stage_attr, None)
        if col is not None:
            stmt = stmt.where(col == stage_token)

    # Filtre « en cours » (analyses uniquement) : ni abandonnée ni convertie.
    if active_only and spec.supports_active:
        status_col = getattr(spec.model, "status", None)
        if status_col is not None:
            stmt = stmt.where(status_col != "abandonne")
        for fk in ("converted_to_deal_id", "converted_to_lead_id"):
            fk_col = getattr(spec.model, fk, None)
            if fk_col is not None:
                stmt = stmt.where(fk_col.is_(None))

    # Tri décroissant sur la colonne déclarée (id en repli).
    order_col = getattr(spec.model, spec.order_attr or "id", None)
    if order_col is None:
        order_col = getattr(spec.model, "id")
    stmt = stmt.order_by(order_col.desc())

    # On lit une ligne de plus que `limit` pour détecter la troncature
    # sans COUNT séparé.
    rows = (
        await db.execute(stmt.offset(offset).limit(limit + 1))
    ).scalars().all()
    truncated = len(rows) > limit
    page = rows[:limit]

    items = [
        serialize_entity(spec.entity_type, obj, level="summary")
        for obj in page
    ]

    return {
        "entity_type": spec.entity_type,
        "pole": spec.pole,
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
    }


@router.get(
    "/entities/{list_type}",
    summary="Lister les entités d'un pôle (vue d'ensemble, clé d'API)",
)
async def list_entities_endpoint(
    ctx: ApiKeyContext,
    db: DBSession,
    list_type: str = Path(
        ...,
        description=(
            "Type de liste : deals, analyses, soumissions, devlog_projects, "
            "entreprises, projects, tal_dossiers, locataire_contacts "
            "(alias acceptés : prospection_deal, lead_analysis, "
            "devlog_project, project, garants…)."
        ),
    ),
    stage: Optional[str] = Query(
        default=None,
        description=(
            "Filtre optionnel par étape / statut (étape pipeline pour les "
            "deals, statut kanban pour les analyses / soumissions / projets)."
        ),
    ),
    active_only: bool = Query(
        default=False,
        description=(
            "Analyses uniquement : ne renvoyer que les fiches « en cours » "
            "(ni abandonnées ni converties)."
        ),
    ),
    limit: int = Query(
        default=_LIST_DEFAULT_LIMIT,
        ge=1,
        le=_LIST_MAX_LIMIT,
        description="Taille de page (1..100, défaut 50).",
    ),
    offset: int = Query(default=0, ge=0, description="Décalage de pagination."),
) -> dict:
    """Renvoie une LISTE paginée (résumés) des entités d'un type pour un
    pôle, en respectant le scope de pôle de la clé. ``truncated=True``
    quand d'autres résultats existent au-delà de la page renvoyée.

    404 si le type est inconnu ; 403 si la clé ne peut pas lire ce pôle."""
    try:
        result = await list_entities(
            db, ctx, list_type,
            stage=stage, active_only=active_only,
            limit=limit, offset=offset,
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Type de liste inconnu : « {list_type} ».",
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        )
    if result.get("truncated"):
        logger.info(
            "list_entities %s tronquée (limit=%s offset=%s) pour clé API",
            list_type, result.get("limit"), result.get("offset"),
        )
    return result