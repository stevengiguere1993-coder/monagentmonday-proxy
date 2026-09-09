"""Smoke — transfert d'unité (points 11-12, retours Phil 2026-09-09).

« Il y a effectivement un nouveau bail s'il change d'unité » : en un
geste, l'ancien bail se termine la veille, un nouveau bail PROPOSÉ est
créé sur la nouvelle unité, le dépôt SUIT le locataire, l'ancienne unité
part en relocation et la nouvelle passe « bail en signature ». Le bail
signé (PDF) rend le nouveau bail actif et referme le dossier.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.models.immobilier import (
    Bail,
    BailStatus,
    Immeuble,
    Locataire,
    LocationDossier,
    LocationDossierStatut,
    Logement,
    LogementStatus,
)

from .conftest import TestSessionLocal

_PDF = b"%PDF-1.4 smoke transfert unite"


def _seed(run, nom: str, *, depot: float = 500.0, externe: bool = False):
    async def _s() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name=nom, address="12 rue Transfert", city="Montréal",
                is_active=True,
            )
            s.add(imm)
            await s.flush()
            lg1 = Logement(
                immeuble_id=imm.id, numero="1",
                status=LogementStatus.OCCUPE.value,
            )
            lg2 = Logement(
                immeuble_id=imm.id, numero="2",
                status=LogementStatus.VACANT.value,
            )
            loc = Locataire(full_name=f"Locataire {nom}")
            s.add_all([lg1, lg2, loc])
            await s.flush()
            b = Bail(
                logement_id=lg1.id, locataire_id=loc.id,
                date_debut=date.today() - timedelta(days=200),
                date_fin=date.today() + timedelta(days=165),
                loyer_mensuel=1000.0,
                depot_garantie=(depot if depot > 0 else None),
                depot_recu_le=(date.today() - timedelta(days=210)) if depot > 0 else None,
                depot_detenteur="MGV" if depot > 0 else None,
                status=BailStatus.ACTIF.value,
            )
            s.add(b)
            await s.flush()
            ext_id = None
            if externe:
                ext = Immeuble(
                    name=f"{nom} externe", address="1 rue Ext",
                    city="Granby", is_active=True, gestion_externe=True,
                )
                s.add(ext)
                await s.flush()
                lg_ext = Logement(
                    immeuble_id=ext.id, numero="E1",
                    status=LogementStatus.VACANT.value,
                )
                s.add(lg_ext)
                await s.flush()
                ext_id = lg_ext.id
            await s.commit()
            return {
                "imm_id": imm.id, "lg1": lg1.id, "lg2": lg2.id,
                "loc_id": loc.id, "bail_id": b.id, "lg_ext": ext_id,
            }

    return run(_s())


def _get(run, model, obj_id: int):
    async def _g():
        async with TestSessionLocal() as s:
            return await s.get(model, obj_id)

    return run(_g())


def _dossier_actif(run, logement_id: int):
    async def _g():
        async with TestSessionLocal() as s:
            return (
                await s.execute(
                    select(LocationDossier).where(
                        LocationDossier.logement_id == logement_id,
                        LocationDossier.statut.notin_(["annule", "reloue"]),
                    )
                )
            ).scalars().first()

    return run(_g())


def test_transfert_complet_le_depot_suit(client, auth_headers, run):
    ids = _seed(run, "Transfert A")
    transfert = date.today() + timedelta(days=10)
    r = client.post(
        f"/api/v1/immobilier/baux/{ids['bail_id']}/transferer",
        headers=auth_headers,
        json={
            "nouveau_logement_id": ids["lg2"],
            "date_transfert": str(transfert),
            "loyer_mensuel": 1300.0,
        },
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["ancien_bail_fin"] == str(transfert - timedelta(days=1))
    assert out["depot_transfere"] == 500.0
    assert out["nouveau_logement_numero"] == "2"

    ancien = _get(run, Bail, ids["bail_id"])
    nouveau = _get(run, Bail, out["nouveau_bail_id"])
    # Ancien bail : fin posée la veille, encore actif (date future),
    # dépôt marqué comme transféré.
    assert ancien.date_fin == transfert - timedelta(days=1)
    assert ancien.status == BailStatus.ACTIF.value
    assert ancien.depot_transfere_vers_bail_id == nouveau.id
    # Nouveau bail : proposé, même locataire, dépôt + date de réception
    # + détenteur repris, fin héritée de l'ancien bail (encore devant).
    assert nouveau.status == BailStatus.PROPOSE.value
    assert nouveau.locataire_id == ids["loc_id"]
    assert nouveau.date_debut == transfert
    assert nouveau.date_fin == date.today() + timedelta(days=165)
    assert float(nouveau.depot_garantie) == 500.0
    assert nouveau.depot_recu_le == date.today() - timedelta(days=210)
    assert nouveau.depot_detenteur == "MGV"
    assert float(nouveau.loyer_mensuel) == 1300.0
    assert "Transfert d'unité depuis le logement 1" in (nouveau.notes or "")

    # Relocation : ancienne unité « à louer », nouvelle « bail en
    # signature » liée au nouveau bail.
    d1 = _dossier_actif(run, ids["lg1"])
    assert d1 is not None and d1.bail_id == ids["bail_id"]
    assert d1.statut == LocationDossierStatut.AVIS_RECU.value
    assert d1.date_depart == transfert - timedelta(days=1)
    d2 = _dossier_actif(run, ids["lg2"])
    assert d2 is not None and d2.id == out["dossier_id"]
    assert d2.statut == LocationDossierStatut.BAIL_ENVOYE.value
    assert d2.nouveau_bail_id == nouveau.id
    assert _get(run, Logement, ids["lg2"]).status == LogementStatus.RESERVE.value

    # Page Dépôts : ancien = « transféré → 2 » (rien à rendre), nouveau
    # = « détenu » (reçu par transfert du 1).
    rd = client.get(
        f"/api/v1/immobilier/depots/overview?immeuble_id={ids['imm_id']}",
        headers=auth_headers,
    )
    assert rd.status_code == 200, rd.text
    par_bail = {x["bail_id"]: x for x in rd.json()["rows"]}
    assert par_bail[ids["bail_id"]]["statut"] == "transfere"
    assert par_bail[ids["bail_id"]]["transfere_vers_logement"] == "2"
    assert par_bail[nouveau.id]["statut"] == "detenu"
    assert par_bail[nouveau.id]["transfere_depuis_logement"] == "1"
    assert rd.json()["total_a_rendre"] == 0
    assert rd.json()["total_rendu"] == 0
    assert rd.json()["total_detenu"] == 500.0

    # Le kanban ne réclame pas le dépôt sortant sur l'ancienne unité.
    ro = client.get(
        f"/api/v1/immobilier/locations/overview?immeuble_id={ids['imm_id']}",
        headers=auth_headers,
    )
    assert ro.status_code == 200, ro.text
    row1 = next(x for x in ro.json()["rows"] if x["id"] == d1.id)
    assert row1["depot_sortant"] is None
    row2 = next(x for x in ro.json()["rows"] if x["id"] == d2.id)
    assert row2["nouveau_locataire_nom"] == "Locataire Transfert A"

    # Bail signé joint → nouveau bail ACTIF, dossier reloué.
    rdoc = client.post(
        f"/api/v1/immobilier/baux/{nouveau.id}/document",
        headers=auth_headers,
        files={"file": ("bail-signe.pdf", _PDF, "application/pdf")},
    )
    assert rdoc.status_code in (200, 201), rdoc.text
    nouveau2 = _get(run, Bail, nouveau.id)
    assert nouveau2.status == BailStatus.ACTIF.value
    assert nouveau2.document_id is not None
    assert _get(run, LocationDossier, d2.id).statut == "reloue"


def test_transfert_immediat_resilie_l_ancien(client, auth_headers, run):
    """Date de transfert aujourd'hui : l'ancien bail est résilié tout de
    suite et l'ancienne unité redevient vacante."""
    ids = _seed(run, "Transfert B", depot=0.0)
    r = client.post(
        f"/api/v1/immobilier/baux/{ids['bail_id']}/transferer",
        headers=auth_headers,
        json={
            "nouveau_logement_id": ids["lg2"],
            "date_transfert": str(date.today()),
            "loyer_mensuel": 1000.0,
            "depot_garantie": 700.0,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["depot_transfere"] == 0.0
    ancien = _get(run, Bail, ids["bail_id"])
    assert ancien.status == BailStatus.RESILIE.value
    assert ancien.depot_transfere_vers_bail_id is None
    assert _get(run, Logement, ids["lg1"]).status == LogementStatus.VACANT.value
    nouveau = _get(run, Bail, r.json()["nouveau_bail_id"])
    assert float(nouveau.depot_garantie) == 700.0
    assert nouveau.depot_recu_le is None


def test_depot_non_transfere_reste_a_rendre(client, auth_headers, run):
    ids = _seed(run, "Transfert C", depot=400.0)
    r = client.post(
        f"/api/v1/immobilier/baux/{ids['bail_id']}/transferer",
        headers=auth_headers,
        json={
            "nouveau_logement_id": ids["lg2"],
            "date_transfert": str(date.today()),
            "loyer_mensuel": 1000.0,
            "transferer_depot": False,
            "depot_garantie": 600.0,
        },
    )
    assert r.status_code == 201, r.text
    rd = client.get(
        f"/api/v1/immobilier/depots/overview?immeuble_id={ids['imm_id']}",
        headers=auth_headers,
    )
    par_bail = {x["bail_id"]: x for x in rd.json()["rows"]}
    assert par_bail[ids["bail_id"]]["statut"] == "a_rendre"
    assert par_bail[r.json()["nouveau_bail_id"]]["statut"] == "detenu"
    assert rd.json()["total_a_rendre"] == 400.0


def test_gardes_du_transfert(client, auth_headers, run):
    ids = _seed(run, "Transfert D", externe=True)
    url = f"/api/v1/immobilier/baux/{ids['bail_id']}/transferer"
    base = {"date_transfert": str(date.today() + timedelta(days=5)), "loyer_mensuel": 900.0}

    # Même logement → 422.
    r = client.post(url, headers=auth_headers, json={**base, "nouveau_logement_id": ids["lg1"]})
    assert r.status_code == 422, r.text
    # Immeuble en gestion externe → 409.
    r = client.post(url, headers=auth_headers, json={**base, "nouveau_logement_id": ids["lg_ext"]})
    assert r.status_code == 409, r.text
    assert "externe" in r.text.lower()
    # Fin avant la date de transfert → 422.
    r = client.post(
        url, headers=auth_headers,
        json={**base, "nouveau_logement_id": ids["lg2"], "date_fin": str(date.today())},
    )
    assert r.status_code == 422, r.text

    # Unité déjà louée sur la période → 409 explicite.
    async def _occuper():
        async with TestSessionLocal() as s:
            autre = Locataire(full_name="Occupant Deja La")
            s.add(autre)
            await s.flush()
            s.add(Bail(
                logement_id=ids["lg2"], locataire_id=autre.id,
                date_debut=date.today() - timedelta(days=30),
                date_fin=date.today() + timedelta(days=300),
                loyer_mensuel=800.0, status=BailStatus.ACTIF.value,
            ))
            await s.commit()

    run(_occuper())
    r = client.post(url, headers=auth_headers, json={**base, "nouveau_logement_id": ids["lg2"]})
    assert r.status_code == 409, r.text
    assert "Occupant Deja La" in r.text

    # Bail non actif → 400.
    async def _terminer():
        async with TestSessionLocal() as s:
            b = await s.get(Bail, ids["bail_id"])
            b.status = BailStatus.TERMINE.value
            await s.commit()

    run(_terminer())
    r = client.post(url, headers=auth_headers, json={**base, "nouveau_logement_id": ids["lg2"]})
    assert r.status_code == 400, r.text


def test_un_seul_transfert_a_la_fois(client, auth_headers, run):
    ids = _seed(run, "Transfert E", depot=0.0)

    async def _troisieme():
        async with TestSessionLocal() as s:
            lg3 = Logement(
                immeuble_id=ids["imm_id"], numero="3",
                status=LogementStatus.VACANT.value,
            )
            s.add(lg3)
            await s.commit()
            return lg3.id

    lg3 = run(_troisieme())
    base = {"date_transfert": str(date.today() + timedelta(days=5)), "loyer_mensuel": 900.0}
    r = client.post(
        f"/api/v1/immobilier/baux/{ids['bail_id']}/transferer",
        headers=auth_headers, json={**base, "nouveau_logement_id": ids["lg2"]},
    )
    assert r.status_code == 201, r.text
    # L'ancien bail est encore actif (transfert futur) : un second
    # transfert est refusé tant que le bail proposé n'est pas réglé.
    r2 = client.post(
        f"/api/v1/immobilier/baux/{ids['bail_id']}/transferer",
        headers=auth_headers, json={**base, "nouveau_logement_id": lg3},
    )
    assert r2.status_code == 409, r2.text
    assert "proposé" in r2.text.lower()
