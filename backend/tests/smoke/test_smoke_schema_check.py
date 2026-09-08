"""Smoke — garde-fous de schéma (incident prod 2026-09-08).

- /health répond 200 + ``schema.ok`` quand la base a toutes les
  colonnes des modèles ;
- /health répond 503 « degraded » dès qu'une colonne manque ;
- ``ajouter_colonnes_manquantes`` crée les colonnes NULLABLE absentes et
  refuse une colonne NOT NULL sans défaut.
"""
from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, String, Table, text

from app.db import schema_check
from app.db.session import engine


def test_health_schema_ok(client, run):
    schema_check._CACHE["result"] = None
    r = client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "healthy"
    assert body["schema"]["ok"] is True
    assert body["schema"]["colonnes_manquantes"] == []


def test_health_degraded_si_colonne_manquante(client, monkeypatch):
    async def _faux(metadata=None):
        return (["lead_analyses.colonne_fantome"], [])

    monkeypatch.setattr(schema_check, "colonnes_manquantes", _faux)
    schema_check._CACHE["result"] = None
    r = client.get("/health")
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["status"] == "degraded"
    assert body["schema"]["colonnes_manquantes"] == ["lead_analyses.colonne_fantome"]
    schema_check._CACHE["result"] = None


def test_ajout_automatique_des_colonnes_nullable(run):
    md = MetaData()
    Table(
        "zz_schema_check_demo", md,
        Column("id", Integer, primary_key=True),
        Column("extra", String(10), nullable=True),
        Column("obligatoire", Integer, nullable=False),
    )

    async def _prepare():
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS zz_schema_check_demo"))
            await conn.execute(
                text("CREATE TABLE zz_schema_check_demo (id INTEGER PRIMARY KEY)")
            )

    run(_prepare())
    manquantes, tables = run(schema_check.colonnes_manquantes(md))
    assert set(manquantes) == {
        "zz_schema_check_demo.extra", "zz_schema_check_demo.obligatoire"
    }
    assert tables == []

    ajoutees = run(schema_check.ajouter_colonnes_manquantes(md))
    # La nullable est ajoutée ; la NOT NULL sans défaut exige une migration.
    assert ajoutees == ["zz_schema_check_demo.extra"]
    manquantes2, _ = run(schema_check.colonnes_manquantes(md))
    assert manquantes2 == ["zz_schema_check_demo.obligatoire"]

    async def _cleanup():
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS zz_schema_check_demo"))

    run(_cleanup())
