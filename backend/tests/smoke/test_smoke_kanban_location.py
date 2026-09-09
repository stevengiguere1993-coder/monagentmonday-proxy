"""Smoke — kanban Locations SIMPLIFIÉ (retour Phil 2026-09-09).

Quatre étapes : « À louer » (avis_recu) → « Affiché » (annonce_publiee)
→ « Bail en signature » (bail_envoye, un locataire est LIÉ) → « Reloué ».
Plus d'annonces, de visites, de candidats ni d'enquêtes dans Kratos.

Ce fichier verrouille :
- lier un locataire EXISTANT ou en CRÉER un sur place → bail proposé,
  carte « Bail en signature », logement réservé ;
- avancer sans locataire → refus qui dit où cliquer (ou réparation d'un
  bail proposé orphelin, impasse de 2026-08-19) ;
- reculer une carte avec un locataire lié → refus actionnable (plus de
  bail orphelin fabriqué en silence) ;
- « Retirer le locataire » → bail proposé supprimé, retour « À louer » ;
- anciennes étapes (visites, candidat retenu, bail à envoyer) traduites
  à la lecture, à l'écriture et migrées par le recalage quotidien.
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


def _seeder(nom: str, statut: str, lier: bool, avec_bail: bool = True):
    async def _seed() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name=nom, address="40 rue Kanban", city="Montréal",
                is_active=True,
            )
            s.add(imm)
            await s.flush()
            lg = Logement(
                immeuble_id=imm.id, numero="1",
                status=LogementStatus.VACANT.value,
            )
            loc = Locataire(full_name="Locataire Existant")
            s.add_all([lg, loc])
            await s.flush()
            bail_id = None
            if avec_bail:
                b = Bail(
                    logement_id=lg.id, locataire_id=loc.id,
                    date_debut=date.today() + timedelta(days=15),
                    date_fin=date.today() + timedelta(days=380),
                    loyer_mensuel=1200.0,
                    status=BailStatus.PROPOSE.value,
                )
                s.add(b)
                await s.flush()
                bail_id = b.id
            d = LocationDossier(
                logement_id=lg.id, statut=statut,
                nouveau_bail_id=(bail_id if lier else None),
            )
            s.add(d)
            await s.flush()
            await s.commit()
            return {
                "dossier_id": d.id, "bail_id": bail_id, "lg_id": lg.id,
                "loc_id": loc.id, "imm_id": imm.id,
            }

    return _seed


def _bail_json(**extra) -> dict:
    return {
        "date_debut": str(date.today() + timedelta(days=10)),
        "date_fin": str(date.today() + timedelta(days=375)),
        "loyer_mensuel": 1150.0,
        **extra,
    }


def _dossier_row(client, auth_headers, imm_id: int, dossier_id: int):
    r = client.get(
        f"/api/v1/immobilier/locations/overview?immeuble_id={imm_id}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    return next(x for x in r.json()["rows"] if x["id"] == dossier_id), r.json()


# ─── Lier un locataire ──────────────────────────────────────────────────


def test_lier_un_locataire_existant(client, auth_headers, run):
    """Depuis « Affiché », lier un locataire déjà client : aucune fiche
    créée, bail proposé, carte « Bail en signature », logement réservé."""
    ids = run(_seeder(
        "Kanban Lier Existant",
        LocationDossierStatut.ANNONCE_PUBLIEE.value, False, avec_bail=False,
    )())
    r = client.post(
        f"/api/v1/immobilier/locations/{ids['dossier_id']}/lier-locataire",
        headers=auth_headers,
        json=_bail_json(locataire_id=ids["loc_id"]),
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["locataire_id"] == ids["loc_id"], "pas de doublon de fiche"

    row, overview = _dossier_row(
        client, auth_headers, ids["imm_id"], ids["dossier_id"]
    )
    assert row["statut"] == LocationDossierStatut.BAIL_ENVOYE.value
    assert row["nouveau_bail_id"] == out["bail_id"]
    assert row["nouveau_locataire_nom"] == "Locataire Existant"
    assert row["nouveau_bail_document_id"] is None, "bail pas encore signé"
    assert row["nouveau_bail_loyer"] == 1150.0
    assert overview["nb_en_signature"] >= 1
    # Plus d'annonces ni de visites exposées.
    assert "annonces" not in row and "visites" not in row

    async def _lire():
        async with TestSessionLocal() as s:
            lg = await s.get(Logement, ids["lg_id"])
            b = await s.get(Bail, out["bail_id"])
            return lg.status, b.status

    assert run(_lire()) == (
        LogementStatus.RESERVE.value, BailStatus.PROPOSE.value
    )

    # Une seconde liaison est refusée : un locataire est déjà lié.
    r2 = client.post(
        f"/api/v1/immobilier/locations/{ids['dossier_id']}/convertir",
        headers=auth_headers,
        json=_bail_json(locataire_id=ids["loc_id"]),
    )
    assert r2.status_code == 409, r2.text


def test_lier_en_creant_le_locataire_sur_place(client, auth_headers, run):
    """Mode « nouveau locataire » : identité obligatoire (nom, naissance,
    courriel, téléphone), la fiche est créée et marquée comme telle."""
    ids = run(_seeder(
        "Kanban Lier Nouveau",
        LocationDossierStatut.AVIS_RECU.value, False, avec_bail=False,
    )())
    url = f"/api/v1/immobilier/locations/{ids['dossier_id']}/lier-locataire"
    # Sans identité → 422 explicite.
    r0 = client.post(
        url, headers=auth_headers, json=_bail_json(locataire_nom="Nouveau"),
    )
    assert r0.status_code == 422, r0.text
    r = client.post(
        url,
        headers=auth_headers,
        json=_bail_json(
            locataire_nom="Nouvelle Locataire",
            locataire_email="nouvelle@test.local",
            locataire_phone="514 555-1234",
            date_naissance="1992-03-03",
        ),
    )
    assert r.status_code == 201, r.text
    loc_id = r.json()["locataire_id"]
    assert loc_id != ids["loc_id"]

    async def _lire():
        async with TestSessionLocal() as s:
            lo = await s.get(Locataire, loc_id)
            d = await s.get(LocationDossier, ids["dossier_id"])
            return lo.full_name, bool(d.locataire_cree), d.statut

    assert run(_lire()) == (
        "Nouvelle Locataire", True, LocationDossierStatut.BAIL_ENVOYE.value
    )


def test_le_bail_herite_du_loue_en_chambres(client, auth_headers, run):
    """Un logement loué en chambres donne un bail AU MOIS sans qu'on ait
    à le redemander (règle 2026-08-19 conservée)."""
    async def _seed() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name="Kanban Chambres", address="42 rue Chambre",
                city="Montréal", is_active=True,
            )
            s.add(imm)
            await s.flush()
            lg = Logement(
                immeuble_id=imm.id, numero="1",
                status=LogementStatus.VACANT.value,
                location_en_chambres=True,
            )
            s.add(lg)
            await s.flush()
            d = LocationDossier(
                logement_id=lg.id,
                statut=LocationDossierStatut.ANNONCE_PUBLIEE.value,
            )
            s.add(d)
            await s.flush()
            await s.commit()
            return {"dossier_id": d.id, "logement_id": lg.id}

    ids = run(_seed())
    r = client.post(
        f"/api/v1/immobilier/locations/{ids['dossier_id']}/lier-locataire",
        headers=auth_headers,
        json=_bail_json(
            locataire_nom="Chambreur Test",
            locataire_email="chambre@test.local",
            locataire_phone="514 555-0000",
            date_naissance="1990-05-14",
            loyer_mensuel=700.0,
        ),
    )
    assert r.status_code == 201, r.text

    async def _lire() -> bool:
        async with TestSessionLocal() as s:
            b = (
                await s.execute(
                    select(Bail).where(Bail.logement_id == ids["logement_id"])
                )
            ).scalars().first()
            return bool(b and b.au_mois)

    assert run(_lire()) is True


# ─── Transitions du kanban ──────────────────────────────────────────────


def test_avancer_sans_locataire_dit_ou_cliquer(client, auth_headers, run):
    ids = run(_seeder(
        "Kanban Sans Loc",
        LocationDossierStatut.ANNONCE_PUBLIEE.value, False, avec_bail=False,
    )())
    r = client.patch(
        f"/api/v1/immobilier/locations/{ids['dossier_id']}",
        headers=auth_headers,
        json={"statut": LocationDossierStatut.BAIL_ENVOYE.value},
    )
    assert r.status_code == 422, r.text
    assert "lier un locataire" in r.text.lower()


def test_bail_orphelin_est_rattache_au_lieu_de_bloquer(
    client, auth_headers, run
):
    """L'état trouvé en prod (2026-08-19) : lien perdu, bail proposé
    orphelin. Avancer doit RÉPARER, pas refuser."""
    ids = run(_seeder(
        "Kanban Orphelin", LocationDossierStatut.ANNONCE_PUBLIEE.value, False,
    )())
    r = client.patch(
        f"/api/v1/immobilier/locations/{ids['dossier_id']}",
        headers=auth_headers,
        json={"statut": LocationDossierStatut.BAIL_ENVOYE.value},
    )
    assert r.status_code == 200, r.text
    assert r.json()["nouveau_bail_id"] == ids["bail_id"]


def test_un_bail_revendique_par_un_autre_dossier_n_est_pas_vole(
    client, auth_headers, run
):
    ids = run(_seeder(
        "Kanban Vol", LocationDossierStatut.ANNONCE_PUBLIEE.value, False,
    )())

    async def _revendiquer() -> int:
        async with TestSessionLocal() as s:
            autre = LocationDossier(
                logement_id=ids["lg_id"],
                statut=LocationDossierStatut.BAIL_ENVOYE.value,
                nouveau_bail_id=ids["bail_id"],
            )
            s.add(autre)
            await s.commit()
            return autre.id

    run(_revendiquer())
    r = client.patch(
        f"/api/v1/immobilier/locations/{ids['dossier_id']}",
        headers=auth_headers,
        json={"statut": LocationDossierStatut.BAIL_ENVOYE.value},
    )
    assert r.status_code == 422, r.text
    assert "lier un locataire" in r.text.lower()


def test_reculer_avec_un_locataire_lie_est_refuse(client, auth_headers, run):
    """Plus de bail orphelin fabriqué en silence : reculer une carte qui
    a un locataire lié est refusé, et le refus dit quoi faire."""
    ids = run(_seeder(
        "Kanban Recul", LocationDossierStatut.BAIL_ENVOYE.value, True,
    )())
    r = client.patch(
        f"/api/v1/immobilier/locations/{ids['dossier_id']}",
        headers=auth_headers,
        json={"statut": LocationDossierStatut.ANNONCE_PUBLIEE.value},
    )
    assert r.status_code == 409, r.text
    assert "retirer le locataire" in r.text.lower()
    row, _ = _dossier_row(client, auth_headers, ids["imm_id"], ids["dossier_id"])
    assert row["nouveau_bail_id"] == ids["bail_id"], "le lien survit"


def test_retirer_le_locataire(client, auth_headers, run):
    """« Retirer le locataire » : bail proposé supprimé, fiche d'un client
    EXISTANT conservée, retour « À louer »."""
    ids = run(_seeder(
        "Kanban Retrait", LocationDossierStatut.BAIL_ENVOYE.value, True,
    )())
    r = client.post(
        f"/api/v1/immobilier/locations/{ids['dossier_id']}/desistement",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == LocationDossierStatut.AVIS_RECU.value
    assert r.json()["nouveau_bail_id"] is None

    async def _lire():
        async with TestSessionLocal() as s:
            return (
                await s.get(Bail, ids["bail_id"]),
                await s.get(Locataire, ids["loc_id"]),
            )

    bail, loc = run(_lire())
    assert bail is None, "le bail proposé est supprimé"
    assert loc is not None, "la fiche d'un client existant reste"


# ─── Anciennes étapes ───────────────────────────────────────────────────


def test_anciennes_etapes_traduites_et_migrees(client, auth_headers, run):
    """Lignes encore en « visites » / « candidat retenu » / « bail à
    envoyer » : lues comme « Affiché » ou « Bail en signature » (selon
    qu'un bail est lié), acceptées à l'écriture, migrées par le
    recalage quotidien."""
    a = run(_seeder("Kanban Legacy A", "visites", False, avec_bail=False)())
    b = run(_seeder("Kanban Legacy B", "candidat_retenu", True)())
    c = run(_seeder("Kanban Legacy C", "bail_a_envoyer", True)())

    row_a, _ = _dossier_row(client, auth_headers, a["imm_id"], a["dossier_id"])
    row_b, _ = _dossier_row(client, auth_headers, b["imm_id"], b["dossier_id"])
    row_c, _ = _dossier_row(client, auth_headers, c["imm_id"], c["dossier_id"])
    assert row_a["statut"] == LocationDossierStatut.ANNONCE_PUBLIEE.value
    assert row_b["statut"] == LocationDossierStatut.BAIL_ENVOYE.value
    assert row_c["statut"] == LocationDossierStatut.BAIL_ENVOYE.value

    # Un vieux client qui envoie « candidat_retenu » sur un dossier
    # sans bail est compris comme « Affiché ».
    r = client.patch(
        f"/api/v1/immobilier/locations/{a['dossier_id']}",
        headers=auth_headers,
        json={"statut": "candidat_retenu"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == LocationDossierStatut.ANNONCE_PUBLIEE.value

    from app.services.locatif_recalage import recalage_quotidien

    async def _recaler():
        async with TestSessionLocal() as s:
            out = await recalage_quotidien(s)
            return (
                out,
                (await s.get(LocationDossier, b["dossier_id"])).statut,
                (await s.get(LocationDossier, c["dossier_id"])).statut,
            )

    out, sb, sc = run(_recaler())
    assert out["dossiers_statuts_migres"] >= 2
    assert sb == LocationDossierStatut.BAIL_ENVOYE.value
    assert sc == LocationDossierStatut.BAIL_ENVOYE.value


def test_les_routes_annonces_et_visites_ont_disparu(client, auth_headers, run):
    ids = run(_seeder(
        "Kanban Routes", LocationDossierStatut.AVIS_RECU.value, False,
        avec_bail=False,
    )())
    r = client.post(
        f"/api/v1/immobilier/locations/{ids['dossier_id']}/annonces",
        headers=auth_headers, json={"plateforme": "Kijiji"},
    )
    assert r.status_code in (404, 405), r.text
    r2 = client.post(
        f"/api/v1/immobilier/locations/{ids['dossier_id']}/visites",
        headers=auth_headers, json={"candidat_nom": "X"},
    )
    assert r2.status_code in (404, 405), r2.text
