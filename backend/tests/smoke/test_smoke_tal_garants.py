"""Smoke — Dossier TAL simple + garants/contacts (retours Phil 2026-09-09).

Point 5 — Dossier TAL :
1. Le PATCH bail qui pose ``tal_dossier_ouvert_le`` (bouton TAL des
   pages Paiements / fiche immeuble) crée un dossier non-paiement
   ouvert (MIROIR), listé sur le locataire ; ``null`` le ferme.
2. Un document importé avec ``tal_dossier_id`` est rattaché au dossier
   ET reste au dossier du locataire (pas de second stockage).
3. Créer / fermer un dossier par ses propres endpoints met la date du
   bail à jour dans l'autre sens.

Point 8 — Garants & contacts :
4. CRUD des contacts d'un locataire.
5. La recherche « Jacques » trouve le locataire Sébastien avec
   ``match_via`` (accents insensibles).
6. /loyers/overview expose ``garants`` et ``payeur_nom``.
7. La recherche globale du topbar renvoie le kind « locataire ».
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.immobilier import (
    Bail,
    BailStatus,
    Immeuble,
    Locataire,
    Logement,
    LogementStatus,
)

from .conftest import TestSessionLocal

_PDF = b"%PDF-1.4 smoke tal"
MOIS = "2026-09"


@pytest.fixture(scope="module")
def tal_seed(run, seeded_users) -> dict:
    async def _seed() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name="Immeuble Smoke TAL", address="5 rue Tribunal",
                is_active=True,
            )
            s.add(imm)
            await s.flush()
            lg = Logement(
                immeuble_id=imm.id, numero="7",
                status=LogementStatus.OCCUPE.value,
            )
            loc = Locataire(
                full_name="Sébastien Tremblay",
                email="seb.tremblay@example.com",
                phone="514 555-0199",
            )
            s.add_all([lg, loc])
            await s.flush()
            bail = Bail(
                logement_id=lg.id,
                locataire_id=loc.id,
                date_debut=date(2026, 7, 1),
                date_fin=date(2026, 7, 1) + timedelta(days=365),
                loyer_mensuel=1200.0,
                status=BailStatus.ACTIF.value,
            )
            s.add(bail)
            await s.commit()
            return {
                "immeuble_id": imm.id,
                "logement_id": lg.id,
                "locataire_id": loc.id,
                "bail_id": bail.id,
            }

    return run(_seed())


def _bail_date(run, bail_id: int):
    async def _get():
        async with TestSessionLocal() as s:
            return (await s.get(Bail, bail_id)).tal_dossier_ouvert_le

    return run(_get())


# ─── Point 5 : dossier TAL ──────────────────────────────────────────────


def test_patch_bail_cree_puis_ferme_le_dossier(client, auth_headers, tal_seed, run):
    bail_id = tal_seed["bail_id"]
    loc_id = tal_seed["locataire_id"]

    # Aucun dossier au départ.
    r = client.get(
        f"/api/v1/immobilier/tal-dossiers?locataire_id={loc_id}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json() == []

    # Le geste historique (bouton TAL) → un dossier non-paiement ouvert.
    r = client.patch(
        f"/api/v1/immobilier/baux/{bail_id}",
        headers=auth_headers,
        json={"tal_dossier_ouvert_le": "2026-09-01"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tal_dossier_ouvert_le"] == "2026-09-01"

    dossiers = client.get(
        f"/api/v1/immobilier/baux/{bail_id}/tal-dossiers", headers=auth_headers
    ).json()
    assert len(dossiers) == 1
    d = dossiers[0]
    assert d["motif"] == "non_paiement"
    assert d["statut"] == "ouvert"
    assert d["ouvert_le"] == "2026-09-01"
    assert d["locataire_id"] == loc_id
    assert d["immeuble_id"] == tal_seed["immeuble_id"]
    assert d["logement_id"] == tal_seed["logement_id"]
    assert d["locataire_name"] == "Sébastien Tremblay"
    assert d["immeuble_name"] == "Immeuble Smoke TAL"

    # Listé sur le locataire (fiche) et via le filtre « en cours ».
    sur_loc = client.get(
        f"/api/v1/immobilier/tal-dossiers?locataire_id={loc_id}&statut=en_cours",
        headers=auth_headers,
    ).json()
    assert [x["id"] for x in sur_loc] == [d["id"]]

    # Re-poser une date n'ouvre PAS un second dossier.
    r = client.patch(
        f"/api/v1/immobilier/baux/{bail_id}",
        headers=auth_headers,
        json={"tal_dossier_ouvert_le": "2026-09-05"},
    )
    assert r.status_code == 200, r.text
    assert len(
        client.get(
            f"/api/v1/immobilier/baux/{bail_id}/tal-dossiers",
            headers=auth_headers,
        ).json()
    ) == 1

    # null → le dossier passe à « ferme », la date du bail s'efface.
    r = client.patch(
        f"/api/v1/immobilier/baux/{bail_id}",
        headers=auth_headers,
        json={"tal_dossier_ouvert_le": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tal_dossier_ouvert_le"] is None
    ferme = client.get(
        f"/api/v1/immobilier/tal-dossiers/{d['id']}", headers=auth_headers
    ).json()
    assert ferme["statut"] == "ferme"
    assert client.get(
        f"/api/v1/immobilier/tal-dossiers?locataire_id={loc_id}&statut=en_cours",
        headers=auth_headers,
    ).json() == []
    assert _bail_date(run, bail_id) is None


def test_creer_et_fermer_par_les_endpoints_dossier(client, auth_headers, tal_seed, run):
    """L'autre sens du miroir : POST dossier → date sur le bail ; PATCH
    statut ferme → date effacée."""
    bail_id = tal_seed["bail_id"]
    r = client.post(
        f"/api/v1/immobilier/baux/{bail_id}/tal-dossiers",
        headers=auth_headers,
        json={
            "motif": "reprise",
            "statut": "a_ouvrir",
            "numero_dossier": "  TAL-2026-001 ",
            "ouvert_le": "2026-09-08",
            "notes": "Reprise pour le fils",
        },
    )
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["numero_dossier"] == "TAL-2026-001"
    assert d["motif"] == "reprise"
    assert _bail_date(run, bail_id) == date(2026, 9, 8)

    # Motif inconnu refusé.
    bad = client.post(
        f"/api/v1/immobilier/baux/{bail_id}/tal-dossiers",
        headers=auth_headers,
        json={"motif": "vengeance"},
    )
    assert bad.status_code == 422

    # Édition en place : audience → la date reste ; ferme → effacée.
    r = client.patch(
        f"/api/v1/immobilier/tal-dossiers/{d['id']}",
        headers=auth_headers,
        json={"statut": "audience", "audience_le": "2026-11-15"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["audience_le"] == "2026-11-15"
    assert _bail_date(run, bail_id) == date(2026, 9, 8)

    r = client.patch(
        f"/api/v1/immobilier/tal-dossiers/{d['id']}",
        headers=auth_headers,
        json={"statut": "ferme", "decision_le": "2026-12-01"},
    )
    assert r.status_code == 200, r.text
    assert _bail_date(run, bail_id) is None

    # Filtre statut inconnu → 422 ; filtre immeuble → trouve les deux.
    assert client.get(
        "/api/v1/immobilier/tal-dossiers?statut=nimporte", headers=auth_headers
    ).status_code == 422
    par_imm = client.get(
        f"/api/v1/immobilier/tal-dossiers?immeuble_id={tal_seed['immeuble_id']}",
        headers=auth_headers,
    ).json()
    assert len(par_imm) == 2


def test_document_rattache_au_dossier(client, auth_headers, tal_seed):
    bail_id = tal_seed["bail_id"]
    loc_id = tal_seed["locataire_id"]
    d = client.post(
        f"/api/v1/immobilier/baux/{bail_id}/tal-dossiers",
        headers=auth_headers,
        json={"motif": "non_paiement"},
    ).json()

    # Import avec le SEUL rattachement tal_dossier_id → bail/locataire
    # déduits du dossier.
    r = client.post(
        "/api/v1/immobilier/documents/import",
        headers=auth_headers,
        files={"file": ("mise-en-demeure.pdf", _PDF, "application/pdf")},
        data={"tal_dossier_id": str(d["id"]), "type": "tal_piece",
              "titre": "Mise en demeure"},
    )
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["tal_dossier_id"] == d["id"]
    assert doc["bail_id"] == bail_id
    assert doc["locataire_id"] == loc_id
    assert doc["source"] == "importe"

    # Le détail du dossier liste la pièce ; la liste compte 1.
    detail = client.get(
        f"/api/v1/immobilier/tal-dossiers/{d['id']}", headers=auth_headers
    ).json()
    assert [x["id"] for x in detail["documents"]] == [doc["id"]]
    liste = client.get(
        f"/api/v1/immobilier/baux/{bail_id}/tal-dossiers", headers=auth_headers
    ).json()
    assert next(x for x in liste if x["id"] == d["id"])["nb_documents"] == 1

    # Pas de second stockage : la pièce est dans les Documents du locataire
    # et son PDF s'ouvre par la route habituelle.
    docs_loc = client.get(
        f"/api/v1/immobilier/locataires/{loc_id}/documents?categorie=dossier",
        headers=auth_headers,
    ).json()
    assert any(x["id"] == doc["id"] for x in docs_loc)
    pdf = client.get(
        f"/api/v1/immobilier/documents/{doc['id']}/pdf", headers=auth_headers
    )
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")

    # Dossier inexistant → 404.
    r = client.post(
        "/api/v1/immobilier/documents/import",
        headers=auth_headers,
        files={"file": ("x.pdf", _PDF, "application/pdf")},
        data={"tal_dossier_id": "999999"},
    )
    assert r.status_code == 404

    # Nettoyage : ferme le dossier pour ne pas polluer les tests suivants.
    client.patch(
        f"/api/v1/immobilier/tal-dossiers/{d['id']}",
        headers=auth_headers,
        json={"statut": "ferme"},
    )


# ─── Point 8 : garants & contacts ───────────────────────────────────────


def test_contacts_crud(client, auth_headers, tal_seed):
    loc_id = tal_seed["locataire_id"]
    url = f"/api/v1/immobilier/locataires/{loc_id}/contacts"
    assert client.get(url, headers=auth_headers).json() == []

    r = client.post(
        url,
        headers=auth_headers,
        json={
            "role": "garant",
            "full_name": "  Jacques Roy ",
            "phone": "(438) 555-0100",
            "email": "jacques.roy@example.com",
            "relation": "père",
            "paie_le_loyer": True,
        },
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["full_name"] == "Jacques Roy"
    assert c["paie_le_loyer"] is True
    assert c["actif"] is True
    assert c["created_by_email"]

    r2 = client.post(
        url,
        headers=auth_headers,
        json={"role": "urgence", "full_name": "Émilie Côté", "phone": "514-555-0777"},
    )
    assert r2.status_code == 201, r2.text

    # Rôle inconnu → 422.
    assert client.post(
        url, headers=auth_headers, json={"role": "avocat", "full_name": "X"}
    ).status_code == 422

    # PATCH : relation + désactivation → sort de la liste par défaut.
    r = client.patch(
        f"/api/v1/immobilier/locataire-contacts/{r2.json()['id']}",
        headers=auth_headers,
        json={"relation": "sœur", "actif": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["relation"] == "sœur"
    noms = [x["full_name"] for x in client.get(url, headers=auth_headers).json()]
    assert noms == ["Jacques Roy"]
    assert len(
        client.get(f"{url}?inclure_inactifs=true", headers=auth_headers).json()
    ) == 2

    # DELETE → 204 puis 404.
    cid = r2.json()["id"]
    assert client.delete(
        f"/api/v1/immobilier/locataire-contacts/{cid}", headers=auth_headers
    ).status_code == 204
    assert client.delete(
        f"/api/v1/immobilier/locataire-contacts/{cid}", headers=auth_headers
    ).status_code == 404

    # Locataire inexistant → 404.
    assert client.get(
        "/api/v1/immobilier/locataires/999999/contacts", headers=auth_headers
    ).status_code == 404


def test_recherche_locataire_via_garant(client, auth_headers, tal_seed):
    """« Quand je cherche Jacques, je vois Sébastien » — et sans accents."""
    loc_id = tal_seed["locataire_id"]

    r = client.get("/api/v1/immobilier/locataires?search=jacques", headers=auth_headers)
    assert r.status_code == 200, r.text
    hit = next(x for x in r.json() if x["id"] == loc_id)
    assert hit["match_via"] == "garant : Jacques Roy"

    # Par le nom lui-même (sans accent, majuscules) → pas de match_via.
    r = client.get("/api/v1/immobilier/locataires?search=SEBASTIEN", headers=auth_headers)
    hit = next(x for x in r.json() if x["id"] == loc_id)
    assert hit["match_via"] is None

    # Par le téléphone du garant (chiffres seulement) et par le courriel
    # du locataire.
    r = client.get("/api/v1/immobilier/locataires?search=4385550100", headers=auth_headers)
    assert any(x["id"] == loc_id for x in r.json())
    r = client.get("/api/v1/immobilier/locataires?search=seb.tremblay", headers=auth_headers)
    hit = next(x for x in r.json() if x["id"] == loc_id)
    assert hit["match_via"].startswith("courriel : ")

    # Rien qui ne matche → liste vide (pas de faux positif).
    r = client.get("/api/v1/immobilier/locataires?search=zzzzqqq", headers=auth_headers)
    assert all(x["id"] != loc_id for x in r.json())


def test_overview_expose_garants_et_payeur(client, auth_headers, tal_seed):
    r = client.get(
        f"/api/v1/immobilier/loyers/overview?mois={MOIS}", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    ligne = next(x for x in r.json()["rows"] if x["bail_id"] == tal_seed["bail_id"])
    assert ligne["garants"] == ["Jacques Roy"]
    assert ligne["payeur_nom"] == "Jacques Roy"


def test_recherche_globale_kind_locataire(client, auth_headers, tal_seed):
    loc_id = tal_seed["locataire_id"]
    # Par le garant.
    r = client.get("/api/v1/search?q=Jacques", headers=auth_headers)
    assert r.status_code == 200, r.text
    hit = next(
        h for h in r.json() if h["kind"] == "locataire" and h["id"] == loc_id
    )
    assert hit["href"] == f"/immobilier/locataires/{loc_id}"
    assert hit["subtitle"] == "garant : Jacques Roy"
    # Par le nom du locataire.
    r = client.get("/api/v1/search?q=Tremblay", headers=auth_headers)
    assert any(
        h["kind"] == "locataire" and h["id"] == loc_id for h in r.json()
    )


def test_ia_couverture_et_mcp_listent_les_nouvelles_tables(client, api_key_headers, tal_seed):
    """Règle « IA au courant de tout » : les deux entités se lisent par
    la clé d'API (liste générique) — le cliquet api_ia_couverture est
    vérifié par test_smoke_couverture_api_ia."""
    from app.core.api_ia_couverture import TABLES_COUVERTES

    assert {"imm_tal_dossiers", "imm_locataire_contacts"} <= TABLES_COUVERTES

    for list_type in ("tal_dossiers", "locataire_contacts"):
        r = client.get(
            f"/api/v1/activity/entities/{list_type}", headers=api_key_headers
        )
        assert r.status_code == 200, (list_type, r.text)
        body = r.json()
        assert body["pole"] == "immobilier"
        assert body["count"] >= 1
