"""Smoke — PLUSIEURS documents déposés à la création d'un locataire
(retour Phil 2026-09-09, point 4).

« Lorsque je crée un locataire, je demandais d'importer un bail ; le
gestionnaire voulait pouvoir importer PLUSIEURS documents à ce
moment-là (règlements d'immeuble, autres), pas juste le bail. »

Côté API rien de nouveau : la modale enchaîne ``POST /locataires`` puis
un ``POST /documents/import`` PAR fichier (séquentiel). Ce que le test
verrouille :
1. les types normalisés ``IMM_DOC_TYPES`` existent et contiennent les
   six clés attendues (miroir TS dans doc-types.ts) ;
2. création locataire → 3 imports (bail + reglement_immeuble + autre)
   rattachés au LOCATAIRE → ``GET /locataires/{id}/documents`` renvoie
   les trois avec les bons types, source « importe » ;
3. un type hors liste est conservé tel quel (rétro-compat) ;
4. création inline « locataire + bail dans la foulée » : le fichier de
   type bail passe par ``POST /baux/{id}/document`` (pose
   ``bail.document_id``, active le bail proposé), les autres par
   ``/documents/import`` avec locataire_id ET bail_id → tout ressort
   sur le locataire ET sur le bail.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.api.v1.endpoints.immobilier_documents import (
    IMM_DOC_TYPE_LABELS,
    IMM_DOC_TYPES,
)
from app.models.immobilier import Bail, Immeuble, Logement, LogementStatus

from .conftest import TestSessionLocal

_PDF = b"%PDF-1.4 smoke creation locataire"
_PNG = b"\x89PNG\r\n\x1a\n smoke"


@pytest.fixture(scope="module")
def unite(run, seeded_users) -> dict:
    """Un immeuble + un logement vacant pour le scénario « bail dans la
    foulée »."""

    async def _seed() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name="Immeuble Smoke Création", address="4 rue Dossier",
                is_active=True,
            )
            s.add(imm)
            await s.flush()
            lg = Logement(
                immeuble_id=imm.id, numero="B",
                status=LogementStatus.VACANT.value,
            )
            s.add(lg)
            await s.commit()
            return {"immeuble_id": imm.id, "logement_id": lg.id}

    return run(_seed())


def _importer(client, headers, *, nom, contenu, ctype, data):
    return client.post(
        "/api/v1/immobilier/documents/import",
        headers=headers,
        files={"file": (nom, contenu, ctype)},
        data=data,
    )


def test_types_normalises():
    cles = [k for k, _ in IMM_DOC_TYPES]
    assert cles == [
        "bail", "reglement_immeuble", "assurance", "enquete_credit",
        "piece_identite", "autre",
    ]
    assert IMM_DOC_TYPE_LABELS["reglement_immeuble"] == (
        "Règlements de l'immeuble"
    )
    # Aucun libellé vide, aucune clé au-delà de la colonne String(48).
    assert all(0 < len(k) <= 48 and lib for k, lib in IMM_DOC_TYPES)


def test_creation_locataire_puis_trois_documents(client, auth_headers):
    r = client.post(
        "/api/v1/immobilier/locataires",
        headers=auth_headers,
        json={"full_name": "Paul Multidoc", "email": "paul@multidoc.local"},
    )
    assert r.status_code == 201, r.text
    loc_id = r.json()["id"]

    attendus = [
        ("bail-signe.pdf", _PDF, "application/pdf", "bail"),
        ("reglements.pdf", _PDF, "application/pdf", "reglement_immeuble"),
        ("photo-permis.png", _PNG, "image/png", "autre"),
    ]
    ids: dict[str, int] = {}
    for nom, contenu, ctype, typ in attendus:
        rr = _importer(
            client, auth_headers, nom=nom, contenu=contenu, ctype=ctype,
            data={"locataire_id": str(loc_id), "type": typ},
        )
        assert rr.status_code == 200, rr.text
        d = rr.json()
        assert d["type"] == typ
        assert d["locataire_id"] == loc_id
        assert d["bail_id"] is None  # pas de bail encore : pièce du locataire
        assert d["source"] == "importe"
        assert d["titre"] == nom  # sans titre explicite → nom du fichier
        ids[typ] = d["id"]

    docs = client.get(
        f"/api/v1/immobilier/locataires/{loc_id}/documents?categorie=dossier",
        headers=auth_headers,
    )
    assert docs.status_code == 200, docs.text
    par_id = {x["id"]: x for x in docs.json()}
    assert set(ids.values()) <= set(par_id)
    for typ, did in ids.items():
        assert par_id[did]["type"] == typ
        assert par_id[did]["source"] == "importe"

    # Le PDF déposé se rouvre tel quel.
    pdf = client.get(
        f"/api/v1/immobilier/documents/{ids['bail']}/pdf", headers=auth_headers
    )
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF-")


def test_type_hors_liste_conserve(client, auth_headers):
    """Rétro-compat : la liste normalisée sert aux menus, pas à valider —
    « releve31 », « dpa »… restent acceptés tels quels."""
    r = client.post(
        "/api/v1/immobilier/locataires",
        headers=auth_headers,
        json={"full_name": "Léa Horsliste"},
    )
    assert r.status_code == 201, r.text
    loc_id = r.json()["id"]
    rr = _importer(
        client, auth_headers, nom="releve.pdf", contenu=_PDF,
        ctype="application/pdf",
        data={"locataire_id": str(loc_id), "type": "releve31"},
    )
    assert rr.status_code == 200, rr.text
    assert rr.json()["type"] == "releve31"


def test_creation_inline_bail_dans_la_foulee(client, auth_headers, unite, run):
    """Assigner un locataire (nouveau) à un logement + documents : le
    bail signé passe par /baux/{id}/document, le reste par
    /documents/import (locataire_id + bail_id)."""
    r = client.post(
        "/api/v1/immobilier/locataires",
        headers=auth_headers,
        json={"full_name": "Nadia Inline"},
    )
    assert r.status_code == 201, r.text
    loc_id = r.json()["id"]

    debut = date(2031, 7, 1)
    rb = client.post(
        "/api/v1/immobilier/baux",
        headers=auth_headers,
        json={
            "logement_id": unite["logement_id"],
            "locataire_id": loc_id,
            "date_debut": debut.isoformat(),
            "date_fin": (debut + timedelta(days=364)).isoformat(),
            "loyer_mensuel": 1100.0,
            "status": "propose",
        },
    )
    assert rb.status_code == 201, rb.text
    bail_id = rb.json()["id"]

    # 1) LE bail signé → endpoint dédié (pose document_id, active).
    rbail = client.post(
        f"/api/v1/immobilier/baux/{bail_id}/document",
        headers=auth_headers,
        files={"file": ("bail-signe.pdf", _PDF, "application/pdf")},
        data={"date_entree": debut.isoformat()},
    )
    assert rbail.status_code == 200, rbail.text
    doc_bail = rbail.json()
    assert doc_bail["type"] == "bail"
    assert doc_bail["bail_id"] == bail_id
    assert doc_bail["locataire_id"] == loc_id
    assert doc_bail["titre"] == f"Bail signé {debut.isoformat()}"

    # 2) Les autres pièces → import générique avec les DEUX rattachements.
    rreg = _importer(
        client, auth_headers, nom="reglements.pdf", contenu=_PDF,
        ctype="application/pdf",
        data={
            "locataire_id": str(loc_id), "bail_id": str(bail_id),
            "type": "reglement_immeuble",
        },
    )
    assert rreg.status_code == 200, rreg.text
    assert rreg.json()["bail_id"] == bail_id
    assert rreg.json()["locataire_id"] == loc_id

    async def _etat():
        async with TestSessionLocal() as s:
            b = await s.get(Bail, bail_id)
            return b.document_id, b.status

    document_id, statut = run(_etat())
    assert document_id == doc_bail["id"]
    assert statut == "actif"  # bail proposé + PDF signé = en vigueur

    # Visible des deux côtés : locataire ET bail.
    for url in (
        f"/api/v1/immobilier/locataires/{loc_id}/documents?categorie=dossier",
        f"/api/v1/immobilier/baux/{bail_id}/documents?categorie=dossier",
    ):
        rows = client.get(url, headers=auth_headers)
        assert rows.status_code == 200, rows.text
        types = {x["id"]: x["type"] for x in rows.json()}
        assert types.get(doc_bail["id"]) == "bail"
        assert types.get(rreg.json()["id"]) == "reglement_immeuble"
