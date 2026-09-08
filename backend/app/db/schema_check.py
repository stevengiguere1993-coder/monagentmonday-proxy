"""Contrôle de schéma — le code ne doit JAMAIS servir avec des colonnes
manquantes en base (incident prod 2026-09-08 : une promotion dont les
nouvelles colonnes de ``lead_analyses`` n'existaient pas en base a fait
échouer toutes les lectures de la table → pipeline et fiches « vides »).

Deux garde-fous, indépendants d'``ensure_critical_columns`` (liste
manuelle, donc oubliable) :

1. ``ajouter_colonnes_manquantes()`` — au démarrage : compare TOUTES les
   colonnes des modèles ORM à la base et ajoute (``ADD COLUMN``, une
   transaction par colonne, verrou borné, 3 tentatives) celles qui
   manquent et sont NULLABLE (ou ont un défaut serveur). Une colonne
   NOT NULL sans défaut n'est jamais ajoutée automatiquement : elle
   exige une vraie migration.
2. ``schema_ok()`` — servi par ``/health`` : s'il reste des colonnes
   manquantes, l'API se déclare ``degraded`` (HTTP 503). Render refuse
   alors de basculer le trafic sur la nouvelle version et l'ancienne
   continue de servir : une promotion cassée ne vide plus une page.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from sqlalchemy import MetaData, text

from app.db.session import engine

log = logging.getLogger("db.schema_check")

# Cache court pour /health (Render interroge souvent).
_CACHE: dict = {"at": 0.0, "result": None}
_TTL_SECONDES = 60.0


def _metadata(metadata: Optional[MetaData]) -> MetaData:
    if metadata is not None:
        return metadata
    from app.db.base import Base

    return Base.metadata


async def _colonnes_en_base(conn, dialect: str, tables: list[str]) -> dict[str, set[str]]:
    """``{table: {colonnes}}`` pour les tables demandées (absente → clé
    absente)."""
    out: dict[str, set[str]] = {}
    if dialect == "postgresql":
        rows = await conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema()"
            )
        )
        for t, c in rows:
            out.setdefault(str(t), set()).add(str(c))
        return {t: cols for t, cols in out.items() if t in set(tables)}
    # SQLite (tests) : PRAGMA table_info.
    for t in tables:
        res = await conn.execute(text(f"PRAGMA table_info('{t}')"))
        cols = {str(r[1]) for r in res}
        if cols:
            out[t] = cols
    return out


async def colonnes_manquantes(
    metadata: Optional[MetaData] = None,
) -> tuple[list[str], list[str]]:
    """Retourne ``(colonnes, tables)`` manquantes : ``["table.colonne", …]``
    pour les tables existantes, et la liste des tables absentes (créées
    normalement par ``create_all`` au démarrage)."""
    md = _metadata(metadata)
    noms = [t.name for t in md.sorted_tables]
    colonnes: list[str] = []
    tables_absentes: list[str] = []
    async with engine.connect() as conn:
        existantes = await _colonnes_en_base(conn, conn.dialect.name, noms)
    for table in md.sorted_tables:
        cols_db = existantes.get(table.name)
        if cols_db is None:
            tables_absentes.append(table.name)
            continue
        for col in table.columns:
            if col.name not in cols_db:
                colonnes.append(f"{table.name}.{col.name}")
    return colonnes, tables_absentes


async def ajouter_colonnes_manquantes(
    metadata: Optional[MetaData] = None,
) -> list[str]:
    """Ajoute en base les colonnes NULLABLE (ou à défaut serveur) que les
    modèles déclarent et que la base n'a pas. Une transaction par
    colonne, verrou borné (Postgres), 3 tentatives. Retourne les
    colonnes ajoutées ; journalise en ERREUR celles qui restent."""
    md = _metadata(metadata)
    manquantes, _tables = await colonnes_manquantes(md)
    if not manquantes:
        return []
    ajoutees: list[str] = []
    dialect = engine.dialect
    par_table = {t.name: t for t in md.sorted_tables}
    for ref in manquantes:
        tname, cname = ref.split(".", 1)
        col = par_table[tname].columns[cname]
        if not col.nullable and col.server_default is None:
            log.error(
                "schema_check : %s est NOT NULL sans défaut — migration "
                "manuelle requise, non ajoutée automatiquement.", ref,
            )
            continue
        type_sql = col.type.compile(dialect=dialect)
        defaut = ""
        if col.server_default is not None and getattr(
            col.server_default, "arg", None
        ) is not None:
            arg = col.server_default.arg
            defaut = f" DEFAULT {getattr(arg, 'text', arg)}"
        if_not_exists = "IF NOT EXISTS " if dialect.name == "postgresql" else ""
        stmt = f"ALTER TABLE {tname} ADD COLUMN {if_not_exists}{cname} {type_sql}{defaut}"
        derniere: Optional[Exception] = None
        for tentative in range(1, 4):
            try:
                async with engine.begin() as conn:
                    if dialect.name == "postgresql":
                        await conn.execute(text("SET LOCAL lock_timeout = '15s'"))
                    await conn.execute(text(stmt))
                ajoutees.append(ref)
                log.warning("schema_check : colonne ajoutée %s (%s)", ref, type_sql)
                derniere = None
                break
            except Exception as exc:  # noqa: BLE001
                derniere = exc
                await asyncio.sleep(2.0 * tentative)
        if derniere is not None:
            log.error(
                "schema_check : impossible d'ajouter %s après 3 tentatives : %s",
                ref, derniere,
            )
    _CACHE["result"] = None
    return ajoutees


async def schema_ok(force: bool = False) -> tuple[bool, list[str], list[str]]:
    """``(ok, colonnes_manquantes, tables_absentes)`` — mis en cache 60 s.
    Seules les COLONNES manquantes sur des tables existantes rendent
    l'API ``degraded`` (c'est la classe d'incident visée) ; une table
    absente est signalée sans bloquer (``create_all`` la crée au
    démarrage). Une erreur du contrôle lui-même ne bloque pas."""
    now = time.monotonic()
    if (
        not force
        and _CACHE["result"] is not None
        and now - _CACHE["at"] < _TTL_SECONDES
    ):
        return _CACHE["result"]
    try:
        colonnes, tables = await colonnes_manquantes()
    except Exception as exc:  # noqa: BLE001
        log.warning("schema_check : contrôle impossible (%s)", exc)
        return True, [], []
    result = (len(colonnes) == 0, colonnes, tables)
    _CACHE["at"] = now
    _CACHE["result"] = result
    if colonnes:
        log.error(
            "SCHÉMA INCOMPLET — colonnes manquantes en base : %s",
            ", ".join(colonnes),
        )
    return result
