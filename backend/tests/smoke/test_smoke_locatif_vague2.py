"""Smoke — audit du cycle de départ, vague 2 (2026-08-13).

Couvre les fixes moyens :
(M1) le bail SORTANT porte le statut de relocation dans les fiches
     locataire/logement (lien par ``LocationDossier.bail_id``) ;
(M4) le prix modifié au kanban redescend sur la fiche du logement
     pendant la vacance (miroir ``loyer_demande``) ;
(M7) un bail RESILIE en cours de mois reste dans loyers/overview du
     mois qu'il couvrait, avec badge « Bail terminé le X » — et
     disparaît des mois suivants ;
(M9a) le GET /locations/overview ne crée plus de dossiers — la
     mutation qui rend le logement vacant s'en charge ;
(M9b) un dossier auto-créé jamais pris en charge par un humain est
     exclu des frais de relocation ; le kanban marque la prise en
     charge ;
(M6)  la suppression d'un bail sortant recopie ses repères dans le
     dossier de relocation + note datée.
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
    LogementStatus,
)

from .conftest import TestSessionLocal


def _seed(
    run,
    *,
    numero: str,
    date_debut: date | None = None,
    date_fin: date | None = None,
    loyer: float = 900.0,
    avec_bail: bool = True,
    logement_status: str = LogementStatus.OCCUPE.value,
    **imm_extra,
) -> dict:
    """Immeuble + logement (+ locataire + bail actif). Retourne les ids."""

    async def _go() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name=f"Immeuble V2 {numero}",
                address=f"{numero} rue Vague2",
                is_active=True,
                **imm_extra,
            )
            s.add(imm)
            await s.flush()
            lg = Logement(
                immeuble_id=imm.id, numero=numero, status=logement_status
            )
            s.add(lg)
            await s.flush()
            out = {"immeuble_id": imm.id, "logement_id": lg.id}
            if avec_bail:
                loc = Locataire(full_name=f"Locataire V2 {numero}")
                s.add(loc)
                await s.flush()
                bail = Bail(
                    logement_id=lg.id,
                    locataire_id=loc.id,
                    date_debut=date_debut,
                    date_fin=date_fin,
                    loyer_mensuel=loyer,
                    status=BailStatus.ACTIF.value,
                )
                s.add(bail)
                await s.flush()
                out["locataire_id"] = loc.id
                out["bail_id"] = bail.id
            await s.commit()
            return out

    return run(_go())


def _db_get(run, model, obj_id: int):
    async def _go():
        async with TestSessionLocal() as s:
            return await s.get(model, obj_id)

    return run(_go())


def _dossiers_du_logement(run, logement_id: int):
    async def _go():
        async with TestSessionLocal() as s:
            return (
                await s.execute(
                    select(LocationDossier).where(
                        LocationDossier.logement_id == logement_id
                    )
                )
            ).scalars().all()

    return run(_go())


# ─── (M7) Bail résilié en cours de mois — loyers/overview ─────────────


def test_bail_resilie_mi_mois_reste_dans_le_mois_avec_badge(
    client, auth_headers, run
):
    today = date.today()
    ids = _seed(
        run,
        numero="V2-M7",
        date_debut=today - timedelta(days=300),
        date_fin=today + timedelta(days=65),
        loyer=980.0,
    )
    r = client.post(
        f"/api/v1/immobilier/baux/{ids['bail_id']}/resilier",
        headers=auth_headers,
        json={
            "date_fin": str(today),
            "ouvrir_relocation": True,
            "envoyer_avis": False,
        },
    )
    assert r.status_code == 200, r.text
    bail = _db_get(run, Bail, ids["bail_id"])
    assert bail.status == BailStatus.RESILIE.value

    # Le mois COUVERT garde la ligne, avec le badge « Bail terminé ».
    mois = today.strftime("%Y-%m")
    ov = client.get(
        f"/api/v1/immobilier/loyers/overview?mois={mois}",
        headers=auth_headers,
    )
    assert ov.status_code == 200, ov.text
    lignes = [
        x for x in ov.json()["rows"] if x["bail_id"] == ids["bail_id"]
    ]
    assert len(lignes) == 1
    assert lignes[0]["bail_statut"] == BailStatus.RESILIE.value
    assert lignes[0]["bail_termine_le"] == str(today)
    assert lignes[0]["loyer_mensuel"] == 980.0

    # Le mois SUIVANT est un mois FUTUR : la dette d'un locataire
    # parti se réclame au présent, elle n'est PLUS projetée dans les
    # mois à venir (règle affinée 2026-08-26, retour Phil « pourquoi
    # ces deux éléments en rouge en septembre ? » — remplace la règle
    # du 2026-08-14 pour les mois futurs ; la dette reste visible dans
    # les mois couverts et le mois courant).
    prochain = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    ov2 = client.get(
        f"/api/v1/immobilier/loyers/overview"
        f"?mois={prochain.strftime('%Y-%m')}",
        headers=auth_headers,
    )
    assert ov2.status_code == 200, ov2.text
    assert all(
        x["bail_id"] != ids["bail_id"] for x in ov2.json()["rows"]
    )

    # Le dernier loyer reste encaissable (bail résilié, mois couvert).
    p = client.post(
        "/api/v1/immobilier/paiements",
        headers=auth_headers,
        json={
            "bail_id": ids["bail_id"],
            "mois_couvert": str(today.replace(day=1)),
            "montant": 980.0,
            "paye_le": str(today),
        },
    )
    assert p.status_code == 201, p.text
    # …mais pas un mois HORS de la période du bail.
    hors = (today.replace(day=1) + timedelta(days=64)).replace(day=1)
    p2 = client.post(
        "/api/v1/immobilier/paiements",
        headers=auth_headers,
        json={
            "bail_id": ids["bail_id"],
            "mois_couvert": str(hors),
            "montant": 980.0,
            "paye_le": str(today),
        },
    )
    assert p2.status_code == 400, p2.text

    # Solder la dette restante → le locataire parti DISPARAÎT des mois
    # que son bail ne couvrait pas… (le trop-payé imputé au mois couvert
    # se répartit sur les mois impayés du bail)
    ov3 = client.get(
        f"/api/v1/immobilier/loyers/overview?mois={mois}",
        headers=auth_headers,
    )
    reste = next(
        x for x in ov3.json()["rows"] if x["bail_id"] == ids["bail_id"]
    )["solde_total"]
    if reste > 0:
        p3 = client.post(
            "/api/v1/immobilier/paiements",
            headers=auth_headers,
            json={
                "bail_id": ids["bail_id"],
                "mois_couvert": str(today.replace(day=1)),
                "montant": reste,
                "paye_le": str(today),
            },
        )
        assert p3.status_code == 201, p3.text
    ov4 = client.get(
        f"/api/v1/immobilier/loyers/overview"
        f"?mois={prochain.strftime('%Y-%m')}",
        headers=auth_headers,
    )
    assert not [
        x for x in ov4.json()["rows"] if x["bail_id"] == ids["bail_id"]
    ]
    # …mais reste visible dans le mois qu'il COUVRAIT.
    ov5 = client.get(
        f"/api/v1/immobilier/loyers/overview?mois={mois}",
        headers=auth_headers,
    )
    assert [
        x for x in ov5.json()["rows"] if x["bail_id"] == ids["bail_id"]
    ]


# ─── (M1) Pastille de relocation sur le bail SORTANT ──────────────────


def test_relocation_statut_present_sur_le_bail_sortant(
    client, auth_headers, run
):
    today = date.today()
    ids = _seed(
        run,
        numero="V2-M1",
        date_debut=today - timedelta(days=200),
        date_fin=today + timedelta(days=120),
        loyer=1050.0,
    )

    async def _seed_dossier():
        async with TestSessionLocal() as s:
            d = LocationDossier(
                logement_id=ids["logement_id"],
                bail_id=ids["bail_id"],
                statut="annonce_publiee",
                date_depart=today + timedelta(days=120),
            )
            s.add(d)
            await s.commit()
            return d.id

    dossier_id = run(_seed_dossier())

    # Fiche locataire : le bail du partant porte sa pastille.
    rl = client.get(
        f"/api/v1/immobilier/locataires/{ids['locataire_id']}/dossier",
        headers=auth_headers,
    )
    assert rl.status_code == 200, rl.text
    bail_row = next(
        b for b in rl.json()["baux"] if b["id"] == ids["bail_id"]
    )
    assert bail_row["relocation_statut"] == "annonce_publiee"
    assert bail_row["relocation_dossier_id"] == dossier_id

    # Fiche logement : même pastille.
    rg = client.get(
        f"/api/v1/immobilier/logements/{ids['logement_id']}/dossier",
        headers=auth_headers,
    )
    assert rg.status_code == 200, rg.text
    bail_row2 = next(
        b for b in rg.json()["baux"] if b["id"] == ids["bail_id"]
    )
    assert bail_row2["relocation_statut"] == "annonce_publiee"
    assert bail_row2["relocation_dossier_id"] == dossier_id


# ─── (M4) Miroir loyer demandé kanban → logement vacant ───────────────


def test_prix_kanban_redescend_sur_le_logement_vacant(
    client, auth_headers, run
):
    ids = _seed(
        run,
        numero="V2-M4",
        avec_bail=False,
        logement_status=LogementStatus.VACANT.value,
    )
    r = client.post(
        "/api/v1/immobilier/locations",
        headers=auth_headers,
        json={"logement_id": ids["logement_id"], "loyer_demande": 800.0},
    )
    assert r.status_code == 201, r.text
    dossier_id = r.json()["id"]

    r2 = client.patch(
        f"/api/v1/immobilier/locations/{dossier_id}",
        headers=auth_headers,
        json={"loyer_demande": 875.0},
    )
    assert r2.status_code == 200, r2.text

    lg = _db_get(run, Logement, ids["logement_id"])
    assert lg.status == LogementStatus.VACANT.value
    assert float(lg.loyer_demande) == 875.0


# ─── (M9a) Plus de création de dossiers dans le GET overview ──────────


def test_overview_ne_cree_plus_de_dossier_mais_la_mutation_oui(
    client, auth_headers, run
):
    today = date.today()
    # 1) Un logement VACANT sans dossier : le GET n'en fabrique pas.
    ids_vac = _seed(
        run,
        numero="V2-M9A",
        avec_bail=False,
        logement_status=LogementStatus.VACANT.value,
    )
    ov = client.get(
        "/api/v1/immobilier/locations/overview", headers=auth_headers
    )
    assert ov.status_code == 200, ov.text
    assert _dossiers_du_logement(run, ids_vac["logement_id"]) == []

    # 2) La MUTATION qui rend un logement vacant ouvre le dossier.
    ids = _seed(
        run,
        numero="V2-M9A2",
        date_debut=today - timedelta(days=400),
        date_fin=today - timedelta(days=5),
        loyer=720.0,
    )
    r = client.patch(
        f"/api/v1/immobilier/baux/{ids['bail_id']}",
        headers=auth_headers,
        json={"status": BailStatus.TERMINE.value},
    )
    assert r.status_code == 200, r.text
    lg = _db_get(run, Logement, ids["logement_id"])
    assert lg.status == LogementStatus.VACANT.value
    dossiers = _dossiers_du_logement(run, ids["logement_id"])
    assert len(dossiers) == 1
    assert (dossiers[0].notes or "").startswith("Créé automatiquement")


# ─── (M9b) Dossier auto exclu des frais tant que pas pris en charge ───


def test_dossier_auto_exclu_des_frais_de_relocation(
    client, auth_headers, run
):
    today = date.today()
    ids = _seed(
        run,
        numero="V2-M9B",
        avec_bail=False,
        logement_status=LogementStatus.VACANT.value,
        frais_gestion_actif=True,
        frais_gestion_pct=10.0,
        frais_relocation_logement=300.0,
    )

    async def _seed_dossiers():
        async with TestSessionLocal() as s:
            lg2 = Logement(
                immeuble_id=ids["immeuble_id"],
                numero="V2-M9B-2",
                status=LogementStatus.VACANT.value,
            )
            s.add(lg2)
            await s.flush()
            auto = LocationDossier(
                logement_id=ids["logement_id"],
                statut="reloue",
                reloue_le=today,
                notes="Créé automatiquement — unité vacante.",
            )
            humain = LocationDossier(
                logement_id=lg2.id,
                statut="reloue",
                reloue_le=today,
                notes="Départ confirmé par le locataire",
            )
            s.add_all([auto, humain])
            await s.commit()
            return {"auto_id": auto.id, "humain_id": humain.id}

    dossiers = run(_seed_dossiers())

    fg = client.get(
        "/api/v1/immobilier/frais-gestion", headers=auth_headers
    )
    assert fg.status_code == 200, fg.text
    row = next(
        x
        for x in fg.json()["rows"]
        if x["immeuble_id"] == ids["immeuble_id"]
    )
    reloc_ids = {
        t.get("dossier_id")
        for t in row["a_facturer"]
        if t["type"] == "relocation"
    }
    # Le dossier auto jamais pris en charge n'est PAS facturable ;
    # le dossier « humain » l'est.
    assert dossiers["auto_id"] not in reloc_ids
    assert dossiers["humain_id"] in reloc_ids


def test_kanban_marque_la_prise_en_charge_humaine(
    client, auth_headers, run
):
    ids = _seed(
        run,
        numero="V2-M9B3",
        avec_bail=False,
        logement_status=LogementStatus.VACANT.value,
    )

    async def _seed_auto():
        async with TestSessionLocal() as s:
            d = LocationDossier(
                logement_id=ids["logement_id"],
                statut="avis_recu",
                notes="Créé automatiquement — unité vacante.",
            )
            s.add(d)
            await s.commit()
            return d.id

    dossier_id = run(_seed_auto())

    r = client.patch(
        f"/api/v1/immobilier/locations/{dossier_id}",
        headers=auth_headers,
        json={"statut": "annonce_publiee"},
    )
    assert r.status_code == 200, r.text
    d = _db_get(run, LocationDossier, dossier_id)
    assert "Pris en charge manuellement" in (d.notes or "")


# ─── (M6) Suppression du bail sortant → dossier consigné ──────────────


def test_delete_bail_sortant_consigne_le_dossier(
    client, auth_headers, run
):
    today = date.today()
    ids = _seed(
        run,
        numero="V2-M6",
        date_debut=today - timedelta(days=250),
        date_fin=today + timedelta(days=30),
        loyer=815.0,
    )

    async def _seed_dossier():
        async with TestSessionLocal() as s:
            d = LocationDossier(
                logement_id=ids["logement_id"],
                bail_id=ids["bail_id"],
                statut="avis_recu",
            )
            s.add(d)
            await s.commit()
            return d.id

    dossier_id = run(_seed_dossier())

    r = client.delete(
        f"/api/v1/immobilier/baux/{ids['bail_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 204, r.text

    d = _db_get(run, LocationDossier, dossier_id)
    # Repères recopiés AVANT la suppression (fill-only) + note datée.
    assert d.date_depart == today + timedelta(days=30)
    assert float(d.loyer_ancien) == 815.0
    assert "Bail sortant supprimé le" in (d.notes or "")
    # Le dossier actif existant suffit : pas de doublon auto-créé.
    assert len(_dossiers_du_logement(run, ids["logement_id"])) == 1
