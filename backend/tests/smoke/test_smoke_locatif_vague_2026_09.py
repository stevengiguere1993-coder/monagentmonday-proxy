"""Smoke — retours Phil 2026-09-09 (pôle locatif, vague 1).

- Gestion EXTERNE : nom de locataire sur le logement (affiché dans les
  paiements), solde CUMULATIF de mois en mois, portes fermées (pas de
  bail, pas de relocation), « Départ » = vacant + nom effacé.
- Recalage quotidien : bail échu → terminé + logement vacant ; dossier
  de relocation refermé quand un bail actif est en place ; dossier sur
  unité externe annulé.
- Bail ACTIF assigné → dossier de relocation refermé (le « libre le »
  ne reste plus collé).
- Dépôt : date de réception et détenteur acceptés à la création.
- Logements en double : diagnostic, garde à la création, fusion.
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
    Logement,
)
from tests.smoke.conftest import TestSessionLocal


def _mk(run, *, externe: bool, nb: int = 2, prefix: str = "T"):
    async def _create():
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name=f"Immeuble {prefix} {'externe' if externe else 'interne'}",
                address="1 rue Test", city="Montréal", postal_code="H1H 1H1",
                gestion_externe=externe,
            )
            s.add(imm)
            await s.flush()
            ids = []
            for i in range(nb):
                lg = Logement(
                    immeuble_id=imm.id, numero=f"{prefix}-{i + 1}",
                    status="occupe", loyer_demande=1000 + i * 100,
                )
                s.add(lg)
                await s.flush()
                ids.append(lg.id)
            loc = Locataire(full_name=f"Locataire {prefix}", email=None, phone=None)
            s.add(loc)
            await s.commit()
            await s.refresh(loc)
            return imm.id, ids, loc.id

    return run(_create())


def _mois(d: date) -> str:
    return d.strftime("%Y-%m")


def test_externe_nom_et_solde_cumulatif(client, auth_headers, run):
    imm_id, (lg1, lg2), _loc = _mk(run, externe=True, prefix="EX")
    # Nom du locataire, facultatif, sur le logement.
    r = client.patch(
        f"/api/v1/immobilier/logements/{lg1}", headers=auth_headers,
        json={"locataire_externe_nom": "Marie Tremblay"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["locataire_externe_nom"] == "Marie Tremblay"

    today = date.today().replace(day=1)
    m_prev = (today - timedelta(days=1)).replace(day=1)
    m_prev2 = (m_prev - timedelta(days=1)).replace(day=1)
    # Rapport du gestionnaire : logement 2 payé il y a deux mois (borne
    # d'entrée de l'immeuble), rien pour le logement 1.
    r = client.post(
        "/api/v1/immobilier/paiements-externes", headers=auth_headers,
        json={"logement_id": lg2, "mois": _mois(m_prev2)},
    )
    assert r.status_code in (200, 201), r.text
    r = client.post(
        "/api/v1/immobilier/paiements-externes", headers=auth_headers,
        json={"logement_id": lg2, "mois": _mois(m_prev)},
    )
    assert r.status_code in (200, 201), r.text

    r = client.get(
        f"/api/v1/immobilier/immeubles/{imm_id}/paiements-externes?mois={_mois(today)}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    rows = {x["logement_id"]: x for x in r.json()["rows"]}
    # Logement 1 : rien reçu depuis la borne → solde = 3 mois × 1 000 $
    # (deux mois antérieurs + ce mois), badge solde antérieur.
    assert rows[lg1]["locataire_nom"] == "Marie Tremblay"
    assert rows[lg1]["solde_anterieur"] is True
    assert abs(rows[lg1]["solde_total"] - 3000) < 0.01
    # Logement 2 : deux mois payés au complet, ce mois dû → solde 1 100 $.
    assert rows[lg2]["solde_anterieur"] is False
    assert abs(rows[lg2]["solde_total"] - 1100) < 0.01

    # Paiement partiel sur un mois « payé au complet » : cumul juste.
    r = client.post(
        "/api/v1/immobilier/paiements-externes", headers=auth_headers,
        json={"logement_id": lg2, "mois": _mois(m_prev), "montant": 50},
    )
    assert r.status_code in (200, 201), r.text
    assert abs(float(r.json()["montant"]) - 1150) < 0.01

    # Vue portefeuille : le nom voyage jusqu'à la page Paiements.
    r = client.get(
        f"/api/v1/immobilier/loyers/externes?mois={_mois(today)}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    ext = {x["logement_id"]: x for x in r.json()["rows"]}
    assert ext[lg1]["locataire_nom"] == "Marie Tremblay"
    assert ext[lg1]["solde_anterieur"] is True
    assert "solde_anterieur" in ext[lg2]


def test_externe_portes_fermees_et_depart(client, auth_headers, run):
    imm_id, (lg1, _lg2), loc_id = _mk(run, externe=True, prefix="EX2")
    # Pas de bail dans Kratos pour un immeuble externe.
    r = client.post(
        "/api/v1/immobilier/baux", headers=auth_headers,
        json={
            "logement_id": lg1, "locataire_id": loc_id,
            "date_debut": "2026-07-01", "date_fin": "2027-06-30",
            "loyer_mensuel": 1000, "status": "actif",
        },
    )
    assert r.status_code == 409, r.text
    assert "gestion externe" in r.json()["detail"].lower()
    # Pas de dossier de relocation.
    r = client.post(
        "/api/v1/immobilier/locations", headers=auth_headers,
        json={"logement_id": lg1},
    )
    assert r.status_code == 409, r.text

    # « Départ » : vacant + nom effacé + bail résiduel terminé.
    async def _seed():
        async with TestSessionLocal() as s:
            lg = await s.get(Logement, lg1)
            lg.locataire_externe_nom = "Paul"
            s.add(Bail(
                logement_id=lg1, locataire_id=loc_id,
                date_debut=date(2026, 1, 1), date_fin=date(2027, 6, 30),
                loyer_mensuel=900, status=BailStatus.ACTIF.value,
            ))
            await s.commit()

    run(_seed())
    r = client.patch(
        f"/api/v1/immobilier/logements/{lg1}", headers=auth_headers,
        json={"status": "vacant"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "vacant"
    assert body["locataire_externe_nom"] is None

    async def _check():
        async with TestSessionLocal() as s:
            baux = (
                await s.execute(select(Bail).where(Bail.logement_id == lg1))
            ).scalars().all()
            dossiers = (
                await s.execute(
                    select(LocationDossier).where(LocationDossier.logement_id == lg1)
                )
            ).scalars().all()
            return [b.status for b in baux], len(dossiers)

    statuts, nb_dossiers = run(_check())
    assert statuts == [BailStatus.TERMINE.value]
    assert nb_dossiers == 0


def test_recalage_quotidien(client, auth_headers, run):
    from app.services.locatif_recalage import recalage_quotidien

    imm_i, (li1, li2), loc_i = _mk(run, externe=False, prefix="IN")
    imm_e, (le1, _le2), loc_e = _mk(run, externe=True, prefix="EX3")
    today = date.today()

    async def _seed():
        async with TestSessionLocal() as s:
            # Bail interne échu hier, avec départ annoncé (dossier ouvert) →
            # doit être TERMINÉ et le logement VACANT.
            b1 = Bail(
                logement_id=li1, locataire_id=loc_i,
                date_debut=today - timedelta(days=400),
                date_fin=today - timedelta(days=1),
                loyer_mensuel=1000, status=BailStatus.ACTIF.value,
            )
            s.add(b1)
            await s.flush()
            s.add(LocationDossier(
                logement_id=li1, bail_id=b1.id, statut="avis_recu",
                date_depart=today - timedelta(days=1),
            ))
            # Logement 2 : dossier de relocation resté ouvert alors qu'un
            # NOUVEAU bail actif est en place → doit être refermé.
            b_old = Bail(
                logement_id=li2, locataire_id=loc_i,
                date_debut=today - timedelta(days=800),
                date_fin=today - timedelta(days=100),
                loyer_mensuel=900, status=BailStatus.TERMINE.value,
            )
            s.add(b_old)
            await s.flush()
            s.add(LocationDossier(
                logement_id=li2, bail_id=b_old.id, statut="annonce_publiee",
                date_depart=today - timedelta(days=100),
            ))
            s.add(Bail(
                logement_id=li2, locataire_id=loc_i,
                date_debut=today - timedelta(days=30),
                date_fin=today + timedelta(days=335),
                loyer_mensuel=1100, status=BailStatus.ACTIF.value,
            ))
            # Dossier sur une unité EXTERNE → annulé.
            s.add(LocationDossier(logement_id=le1, statut="avis_recu"))
            await s.commit()

    run(_seed())

    async def _run():
        async with TestSessionLocal() as s:
            return await recalage_quotidien(s)

    out = run(_run())
    assert out["dossiers_externes_annules"] == 1
    assert out["dossiers_refermes"] >= 1

    async def _check():
        async with TestSessionLocal() as s:
            b = (
                await s.execute(select(Bail).where(Bail.logement_id == li1))
            ).scalars().first()
            lg1 = await s.get(Logement, li1)
            d2 = (
                await s.execute(
                    select(LocationDossier).where(LocationDossier.logement_id == li2)
                )
            ).scalars().first()
            de = (
                await s.execute(
                    select(LocationDossier).where(LocationDossier.logement_id == le1)
                )
            ).scalars().first()
            return b.status, lg1.status, d2.statut, d2.nouveau_bail_id, de.statut

    st_bail, st_lg, st_d2, nb2, st_de = run(_check())
    assert st_bail == BailStatus.TERMINE.value
    assert st_lg == "vacant"
    assert st_d2 == "reloue" and nb2 is not None
    assert st_de == "annule"

    # La liste des locataires ne « loge » plus le locataire au bail échu.
    r = client.get("/api/v1/immobilier/locataires?search=Locataire IN", headers=auth_headers)
    assert r.status_code == 200
    item = next(x for x in r.json() if x["id"] == loc_i)
    # Il habite toujours li2 (bail actif en place), pas li1.
    assert item["logement_id"] == li2


def test_bail_actif_referme_le_dossier_et_depot(client, auth_headers, run):
    imm_id, (lg1, _lg2), loc_id = _mk(run, externe=False, prefix="IN2")

    async def _seed():
        async with TestSessionLocal() as s:
            s.add(LocationDossier(
                logement_id=lg1, statut="annonce_publiee",
                date_depart=date(2027, 6, 30),
            ))
            await s.commit()

    run(_seed())
    r = client.post(
        "/api/v1/immobilier/baux", headers=auth_headers,
        json={
            "logement_id": lg1, "locataire_id": loc_id,
            "date_debut": str(date.today()), "date_fin": "2027-06-30",
            "loyer_mensuel": 1200, "status": "actif",
            "depot_garantie": 600, "depot_recu_le": "2026-09-01",
            "depot_detenteur": "Compte en fidéicommis",
        },
    )
    assert r.status_code == 201, r.text
    bail_id = r.json()["id"]

    async def _check():
        async with TestSessionLocal() as s:
            d = (
                await s.execute(
                    select(LocationDossier).where(LocationDossier.logement_id == lg1)
                )
            ).scalars().first()
            b = await s.get(Bail, bail_id)
            return d.statut, d.nouveau_bail_id, str(b.depot_recu_le), b.depot_detenteur

    statut, nb, recu, det = run(_check())
    assert statut == "reloue" and nb == bail_id
    assert recu == "2026-09-01" and det == "Compte en fidéicommis"
    # Plus de « libre le » sur ce logement.
    r = client.get(f"/api/v1/immobilier/immeubles/{imm_id}/logements", headers=auth_headers)
    row = next(x for x in r.json() if x["id"] == lg1)
    assert row["libre_le"] is None
    # La page Dépôts voit la date et le détenteur.
    r = client.get("/api/v1/immobilier/depots/overview", headers=auth_headers)
    assert r.status_code == 200
    dep = next(x for x in r.json()["rows"] if x["bail_id"] == bail_id)
    assert dep["depot_recu_le"] == "2026-09-01"
    assert dep["depot_detenteur"] == "Compte en fidéicommis"


def test_logements_doublons(client, auth_headers, run):
    imm_id, (lg1, lg2), loc_id = _mk(run, externe=False, prefix="DB")

    async def _seed():
        async with TestSessionLocal() as s:
            lg = await s.get(Logement, lg2)
            lg.numero = "8906 - C"
            lg1o = await s.get(Logement, lg1)
            lg1o.numero = "8906-c"
            await s.commit()

    run(_seed())
    # Garde à la création : même numéro normalisé → 409, sauf force.
    r = client.post(
        "/api/v1/immobilier/logements", headers=auth_headers,
        json={"immeuble_id": imm_id, "numero": "8906 C"},
    )
    assert r.status_code == 409, r.text
    r = client.post(
        "/api/v1/immobilier/logements?force=true", headers=auth_headers,
        json={"immeuble_id": imm_id, "numero": "8906 C"},
    )
    assert r.status_code == 201, r.text
    lg3 = r.json()["id"]

    r = client.get("/api/v1/immobilier/logements/doublons", headers=auth_headers)
    assert r.status_code == 200, r.text
    grp = next(g for g in r.json() if g["immeuble_id"] == imm_id)
    assert {x["id"] for x in grp["logements"]} == {lg1, lg2, lg3}

    # Fusion : les baux des doublons suivent, les doublons disparaissent.
    r = client.post(
        "/api/v1/immobilier/baux", headers=auth_headers,
        json={
            "logement_id": lg2, "locataire_id": loc_id,
            "date_debut": "2026-01-01", "date_fin": "2026-12-31",
            "loyer_mensuel": 800, "status": "termine",
        },
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/v1/immobilier/logements/fusionner", headers=auth_headers,
        json={"garder_id": lg1, "supprimer_ids": [lg2, lg3]},
    )
    assert r.status_code == 200, r.text
    r = client.get("/api/v1/immobilier/logements/doublons", headers=auth_headers)
    assert not [g for g in r.json() if g["immeuble_id"] == imm_id]

    async def _check():
        async with TestSessionLocal() as s:
            baux = (await s.execute(select(Bail).where(Bail.logement_id == lg1))).scalars().all()
            restants = (
                await s.execute(select(Logement).where(Logement.immeuble_id == imm_id))
            ).scalars().all()
            return len(baux), [x.id for x in restants]

    nb_baux, restants = run(_check())
    assert nb_baux == 1
    assert restants == [lg1]
