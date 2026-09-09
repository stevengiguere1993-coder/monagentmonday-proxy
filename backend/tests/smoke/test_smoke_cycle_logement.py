"""Smoke — cycle de vie du logement (retours Phil 2026-08-19).

Trois choses que Phil a relevées en testant :

1. un logement restait « réservé » alors que le candidat avait été
   retiré — le statut est DÉRIVÉ des baux mais STOCKÉ, donc il se périme
   dès qu'une transition oublie de le recalculer ;
2. « occupé » ne disait pas qu'un départ était acté pour le 31 août —
   or ce n'est pas le même état, c'est celui-là qu'il faut relouer ;
3. après la date de départ, le locataire ne doit plus être rattaché aux
   loyers du logement.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

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


def test_statut_perime_est_recale_au_demarrage(run, db_setup):
    """Un bail PROPOSÉ dont la date de début est PASSÉE ne réserve plus
    rien : le logement doit redevenir vacant. C'est exactement l'état
    trouvé en prod (bail proposé de 2024 sur un logement « réservé »).
    """
    from app.services.locatif_depart import (
        recaler_tous_les_statuts_logements,
    )

    async def _seed() -> int:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name="Immeuble Statut Perime", address="20 rue Statut",
                city="Montréal", is_active=True,
            )
            s.add(imm)
            await s.flush()
            lg = Logement(
                immeuble_id=imm.id, numero="1",
                # Statut PÉRIMÉ, posé à la main comme en prod.
                status=LogementStatus.RESERVE.value,
            )
            loc = Locataire(full_name="Candidat Retire")
            s.add_all([lg, loc])
            await s.flush()
            s.add(
                Bail(
                    logement_id=lg.id, locataire_id=loc.id,
                    date_debut=date.today() - timedelta(days=400),
                    date_fin=date.today() - timedelta(days=35),
                    loyer_mensuel=900.0,
                    status=BailStatus.PROPOSE.value,
                )
            )
            await s.commit()
            return lg.id

    lg_id = run(_seed())

    async def _recaler() -> str:
        async with TestSessionLocal() as s:
            await recaler_tous_les_statuts_logements(s)
        async with TestSessionLocal() as s:
            lg = await s.get(Logement, lg_id)
            return lg.status

    assert run(_recaler()) == LogementStatus.VACANT.value


def test_libere_le_ne_repose_que_sur_un_depart_acte(run, db_setup):
    """⚠️ Une fin de bail n'est PAS un départ : au Québec le bail se
    reconduit tacitement. Seul un dossier de relocation ouvert libère le
    logement — sinon Kratos annoncerait des vacances imaginaires pour
    tous les baux qui arrivent à échéance.
    """
    from app.services.locatif_depart import libere_le

    async def _seed() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name="Immeuble Libere Le", address="22 rue Depart",
                city="Montréal", is_active=True,
            )
            s.add(imm)
            await s.flush()
            ids = {}
            for numero, avec_dossier in (("1", False), ("2", True)):
                lg = Logement(
                    immeuble_id=imm.id, numero=numero,
                    status=LogementStatus.OCCUPE.value,
                )
                loc = Locataire(full_name=f"Locataire {numero}")
                s.add_all([lg, loc])
                await s.flush()
                b = Bail(
                    logement_id=lg.id, locataire_id=loc.id,
                    date_debut=date.today() - timedelta(days=300),
                    date_fin=date(2026, 8, 31),
                    loyer_mensuel=1000.0,
                    status=BailStatus.ACTIF.value,
                )
                s.add(b)
                await s.flush()
                if avec_dossier:
                    s.add(
                        LocationDossier(
                            logement_id=lg.id, bail_id=b.id,
                            statut=LocationDossierStatut.AVIS_RECU.value,
                            date_depart=date(2026, 8, 31),
                        )
                    )
                ids[numero] = lg.id
            await s.commit()
            return ids

    ids = run(_seed())

    async def _lire() -> tuple:
        async with TestSessionLocal() as s:
            return (
                await libere_le(s, ids["1"]),
                await libere_le(s, ids["2"]),
            )

    sans_dossier, avec_dossier = run(_lire())
    assert sans_dossier is None, (
        "un bail qui arrive à échéance n'annonce PAS une vacance"
    )
    assert avec_dossier == date(2026, 8, 31)


@pytest.mark.parametrize("solde_du", [False, True])
def test_locataire_parti_disparait_des_loyers(
    client, auth_headers, run, solde_du
):
    """Après la date de départ, le locataire n'est plus rattaché aux
    loyers du logement (retour Phil) — SAUF s'il doit encore de
    l'argent : effacer une dette parce que le bail est fini serait pire
    que le laisser apparaître.
    """
    fin = date.today().replace(day=1) - timedelta(days=1)  # fin du mois passé

    async def _seed() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name=f"Immeuble Parti {solde_du}", address="24 rue Parti",
                city="Montréal", is_active=True,
            )
            s.add(imm)
            await s.flush()
            lg = Logement(
                immeuble_id=imm.id, numero="1",
                status=LogementStatus.VACANT.value,
            )
            loc = Locataire(full_name="Ancien Locataire")
            s.add_all([lg, loc])
            await s.flush()
            b = Bail(
                logement_id=lg.id, locataire_id=loc.id,
                date_debut=fin - timedelta(days=364),
                date_fin=fin,
                loyer_mensuel=1000.0,
                status=BailStatus.TERMINE.value,
            )
            s.add(b)
            await s.flush()
            if not solde_du:
                from datetime import datetime, timezone

                from app.models.immobilier import PaiementLoyer
                from app.services.locatif_demarrage import (
                    DEFAULT_DEMARRAGE,
                )

                # « À jour » = TOUS les mois échus depuis le démarrage
                # du pôle sont payés. (Avant, le seed ne payait que le
                # dernier mois : dès que le démarrage avait 2+ mois
                # d'ancienneté — p. ex. le 2026-09-01 — il restait une
                # vraie dette et le test échouait par calendrier.)
                mois = max(b.date_debut, DEFAULT_DEMARRAGE).replace(
                    day=1
                )
                fin_mois = fin.replace(day=1)
                while mois <= fin_mois:
                    s.add(
                        PaiementLoyer(
                            bail_id=b.id, mois_couvert=mois,
                            montant=1000.0, paye_le=mois,
                            created_at=datetime.now(timezone.utc),
                        )
                    )
                    mois = (
                        mois.replace(day=28) + timedelta(days=4)
                    ).replace(day=1)
            await s.commit()
            return {"immeuble_id": imm.id, "bail_id": b.id}

    ids = run(_seed())
    mois_courant = date.today().strftime("%Y-%m")
    r = client.get(
        f"/api/v1/immobilier/loyers/overview?mois={mois_courant}"
        f"&immeuble_id={ids['immeuble_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    presents = [x["bail_id"] for x in r.json()["rows"]]
    if solde_du:
        assert ids["bail_id"] in presents, (
            "une dette doit rester visible même après le départ"
        )
    else:
        assert ids["bail_id"] not in presents, (
            "un locataire parti et à jour ne doit plus apparaître"
        )


def test_annuler_depart_refuse_si_locataire_lie(client, auth_headers, run):
    """Le geste inverse de « mettre fin au bail ». Il doit être REFUSÉ
    dès qu'un locataire est lié (bail en signature) : annuler mettrait
    deux locataires sur la même unité. Le refus dit quoi faire plutôt
    que de se contenter de bloquer.
    """
    async def _seed(statut: str) -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name=f"Immeuble Annul {statut}", address="26 rue Annul",
                city="Montréal", is_active=True,
            )
            s.add(imm)
            await s.flush()
            lg = Logement(
                immeuble_id=imm.id, numero="1",
                status=LogementStatus.OCCUPE.value,
            )
            loc = Locataire(full_name="Locataire Hesitant")
            s.add_all([lg, loc])
            await s.flush()
            b = Bail(
                logement_id=lg.id, locataire_id=loc.id,
                date_debut=date.today() - timedelta(days=200),
                date_fin=date.today() + timedelta(days=100),
                loyer_mensuel=1000.0,
                status=BailStatus.RESILIE.value,
            )
            s.add(b)
            await s.flush()
            s.add(
                LocationDossier(
                    logement_id=lg.id, bail_id=b.id, statut=statut,
                    date_depart=date.today() + timedelta(days=20),
                )
            )
            await s.commit()
            return {"bail_id": b.id, "logement_id": lg.id}

    # (a) Locataire lié (bail en signature) → refus explicite.
    engage = run(_seed(LocationDossierStatut.BAIL_ENVOYE.value))
    r = client.post(
        f"/api/v1/immobilier/baux/{engage['bail_id']}/annuler-depart",
        headers=auth_headers,
    )
    assert r.status_code == 409, r.text
    assert "locataire" in r.text.lower()

    # (b) Départ simplement annoncé → l'annulation passe, le bail
    # redevient actif et le logement occupé.
    libre = run(_seed(LocationDossierStatut.AVIS_RECU.value))
    r2 = client.post(
        f"/api/v1/immobilier/baux/{libre['bail_id']}/annuler-depart",
        headers=auth_headers,
    )
    assert r2.status_code == 200, r2.text
    data = r2.json()
    assert data["bail_reactive"] is True
    assert data["logement_statut"] == LogementStatus.OCCUPE.value

    # Et le logement ne se dit plus « libre le … ».
    from app.services.locatif_depart import libere_le

    async def _lire():
        async with TestSessionLocal() as s:
            return await libere_le(s, libre["logement_id"])

    assert run(_lire()) is None


def test_annuler_depart_sans_depart_en_cours(client, auth_headers, run):
    """Sur un bail sans départ acté, l'annulation n'a aucun sens — 409
    plutôt qu'un succès silencieux qui ne ferait rien."""
    async def _seed() -> int:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name="Immeuble Sans Depart", address="28 rue Calme",
                city="Montréal", is_active=True,
            )
            s.add(imm)
            await s.flush()
            lg = Logement(
                immeuble_id=imm.id, numero="1",
                status=LogementStatus.OCCUPE.value,
            )
            loc = Locataire(full_name="Locataire Tranquille")
            s.add_all([lg, loc])
            await s.flush()
            b = Bail(
                logement_id=lg.id, locataire_id=loc.id,
                date_debut=date.today() - timedelta(days=100),
                date_fin=date.today() + timedelta(days=200),
                loyer_mensuel=1000.0, status=BailStatus.ACTIF.value,
            )
            s.add(b)
            await s.flush()
            await s.commit()
            return b.id

    bail_id = run(_seed())
    r = client.post(
        f"/api/v1/immobilier/baux/{bail_id}/annuler-depart",
        headers=auth_headers,
    )
    assert r.status_code == 409, r.text


def test_suivi_entente_resiliation(client, auth_headers, run):
    """« Si c'est pas signé il est pas encore mis fin » (Phil).

    La ligne rouge disait seulement « signature attendue ». Or une
    entente jamais ouverte se relance, tandis qu'une entente ouverte et
    non signée se discute : ce n'est pas le même geste, donc la ligne
    doit porter les deux horodatages.
    """
    from datetime import datetime, timezone

    from app.models.immobilier import ImmDocument

    async def _seed() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name="Immeuble Entente", address="30 rue Entente",
                city="Montréal", is_active=True,
            )
            s.add(imm)
            await s.flush()
            ids = {"immeuble_id": imm.id}
            for numero, ouvert in (("1", False), ("2", True)):
                lg = Logement(
                    immeuble_id=imm.id, numero=numero,
                    status=LogementStatus.OCCUPE.value,
                )
                loc = Locataire(full_name=f"Locataire Entente {numero}")
                s.add_all([lg, loc])
                await s.flush()
                b = Bail(
                    logement_id=lg.id, locataire_id=loc.id,
                    date_debut=date.today() - timedelta(days=200),
                    date_fin=date.today() + timedelta(days=160),
                    loyer_mensuel=1000.0, status=BailStatus.ACTIF.value,
                )
                s.add(b)
                await s.flush()
                s.add(
                    ImmDocument(
                        bail_id=b.id, locataire_id=loc.id,
                        immeuble_id=imm.id,
                        type="avis_resiliation",
                        titre="Entente de résiliation",
                        params_json='{"date_fin": "2026-10-31"}',
                        envoye_le=datetime(
                            2026, 8, 10, 12, 0, tzinfo=timezone.utc
                        ),
                        ouvert_le=(
                            datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)
                            if ouvert else None
                        ),
                    )
                )
                ids[numero] = b.id
            await s.commit()
            return ids

    ids = run(_seed())
    r = client.get(
        f"/api/v1/immobilier/suivi-baux?immeuble_id={ids['immeuble_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    par_bail = {x["bail_id"]: x for x in r.json() if x.get("bail_id")}

    jamais_ouverte = par_bail[ids["1"]]
    assert jamais_ouverte["resiliation_en_cours"] is True
    assert jamais_ouverte["resiliation_envoye_le"] is not None
    assert jamais_ouverte["resiliation_ouvert_le"] is None
    # La date convenue vient des paramètres du document.
    assert jamais_ouverte["resiliation_date"] == "2026-10-31"

    vue_non_signee = par_bail[ids["2"]]
    assert vue_non_signee["resiliation_ouvert_le"] is not None


def test_suivi_baux_filtre_sans_dupliquer(client, auth_headers, run):
    """« La section des baux d'une fiche doit être exactement pareille
    que dans la page Baux, mais juste pour ce locataire-là » (Phil).

    Donc un FILTRE sur la même donnée, jamais une deuxième
    implémentation : sinon les deux vues divergent, et c'est celle qu'on
    regarde le moins qui finit par mentir.
    """
    async def _seed() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name="Immeuble Miroir", address="60 rue Miroir",
                city="Montréal", is_active=True,
            )
            s.add(imm)
            await s.flush()
            ids = {"immeuble_id": imm.id}
            for numero in ("1", "2"):
                lg = Logement(
                    immeuble_id=imm.id, numero=numero,
                    status=LogementStatus.OCCUPE.value,
                )
                lo = Locataire(full_name=f"Miroir {numero}")
                s.add_all([lg, lo])
                await s.flush()
                b = Bail(
                    logement_id=lg.id, locataire_id=lo.id,
                    date_debut=date.today() - timedelta(days=100),
                    date_fin=date.today() + timedelta(days=200),
                    loyer_mensuel=1000.0, status=BailStatus.ACTIF.value,
                )
                s.add(b)
                await s.flush()
                ids[f"loc{numero}"] = lo.id
                ids[f"lg{numero}"] = lg.id
            await s.commit()
            return ids

    ids = run(_seed())
    base = f"/api/v1/immobilier/suivi-baux?immeuble_id={ids['immeuble_id']}"
    tout = client.get(base, headers=auth_headers).json()
    assert len(tout) == 2

    par_loc = client.get(
        f"{base}&locataire_id={ids['loc1']}", headers=auth_headers
    ).json()
    assert len(par_loc) == 1
    assert par_loc[0]["locataire_id"] == ids["loc1"]
    # MÊME forme de ligne que la page complète — c'est tout l'intérêt.
    assert par_loc[0].keys() == tout[0].keys()

    par_log = client.get(
        f"{base}&logement_id={ids['lg2']}", headers=auth_headers
    ).json()
    assert len(par_log) == 1
    assert par_log[0]["logement_id"] == ids["lg2"]


def test_locataire_parti_garde_son_releve_31(client, auth_headers, run):
    """« Aussi pour le relevé 31 évidemment » (Phil).

    Un locataire parti disparaît du suivi des loyers, mais il DOIT
    garder son relevé 31 : Revenu Québec en exige un par personne ayant
    occupé le logement pendant l'année. Deux occupants successifs = deux
    relevés.

    Vérifié plutôt que « corrigé » : la sélection se fait déjà par
    chevauchement de période, pas sur les baux actifs.
    """
    annee = date.today().year

    async def _seed() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name="Immeuble Releve31", address="80 rue Releve",
                city="Montréal", is_active=True,
            )
            s.add(imm)
            await s.flush()
            lg = Logement(
                immeuble_id=imm.id, numero="1",
                status=LogementStatus.OCCUPE.value,
            )
            parti = Locataire(full_name="Parti En Juin")
            arrive = Locataire(full_name="Arrive En Juillet")
            s.add_all([lg, parti, arrive])
            await s.flush()
            s.add(
                Bail(
                    logement_id=lg.id, locataire_id=parti.id,
                    date_debut=date(annee - 1, 7, 1),
                    date_fin=date(annee, 6, 30),
                    loyer_mensuel=1000.0,
                    status=BailStatus.TERMINE.value,
                )
            )
            s.add(
                Bail(
                    logement_id=lg.id, locataire_id=arrive.id,
                    date_debut=date(annee, 7, 1),
                    date_fin=date(annee + 1, 6, 30),
                    loyer_mensuel=1100.0,
                    status=BailStatus.ACTIF.value,
                )
            )
            await s.commit()
            return {"parti": parti.id, "arrive": arrive.id}

    ids = run(_seed())
    r = client.get(
        f"/api/v1/immobilier/releves31?annee={annee}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    par_loc = {
        x.get("locataire_id") for x in r.json().get("rows", [])
    }
    assert ids["parti"] in par_loc, (
        "le locataire parti doit garder son relevé 31 pour l'année où "
        "il a occupé le logement"
    )
    assert ids["arrive"] in par_loc, "deux occupants = deux relevés"


def test_renouvellements_filtrables_par_fiche(client, auth_headers, run):
    """Même donnée, filtrée — jamais une deuxième implémentation. La
    ligne filtrée doit avoir exactement les mêmes champs que la ligne
    de la page complète, sinon la fiche finirait par montrer autre
    chose (retour Phil 2026-08-19).
    """
    async def _seed() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name="Immeuble Renouv Miroir", address="90 rue Renouv",
                city="Montréal", is_active=True,
            )
            s.add(imm)
            await s.flush()
            ids = {}
            for numero in ("1", "2"):
                lg = Logement(
                    immeuble_id=imm.id, numero=numero,
                    status=LogementStatus.OCCUPE.value,
                )
                lo = Locataire(full_name=f"Renouv {numero}")
                s.add_all([lg, lo])
                await s.flush()
                s.add(
                    Bail(
                        logement_id=lg.id, locataire_id=lo.id,
                        date_debut=date.today() - timedelta(days=200),
                        date_fin=date.today() + timedelta(days=160),
                        loyer_mensuel=1000.0,
                        status=BailStatus.ACTIF.value,
                    )
                )
                ids[numero] = {"loc": lo.id, "lg": lg.id}
            await s.commit()
            return ids

    ids = run(_seed())
    base = "/api/v1/immobilier/renouvellements/overview"
    tout = client.get(base, headers=auth_headers).json()
    assert len(tout) >= 2

    par_loc = client.get(
        f"{base}?locataire_id={ids['1']['loc']}", headers=auth_headers
    ).json()
    assert len(par_loc) == 1
    assert par_loc[0]["locataire_id"] == ids["1"]["loc"]
    assert par_loc[0].keys() == tout[0].keys()

    par_log = client.get(
        f"{base}?logement_id={ids['2']['lg']}", headers=auth_headers
    ).json()
    assert len(par_log) == 1
    assert par_log[0]["logement_id"] == ids["2"]["lg"]


def test_recalage_ne_touche_jamais_la_gestion_externe(run):
    """⚠️ Régression du 2026-08-20, corrigée le jour même.

    En gestion externe, le statut du logement est saisi À LA MAIN : les
    baux de ces immeubles ne sont pas dans Kratos. La règle « pas de
    bail actif → vacant » y est donc un contresens — et son application
    par le recalage global a mis « vacant » les 19 logements de la
    Place Sapinière, tous occupés (leurs paiements d'août le
    prouvaient). Un locataire réel a « disparu » de la page Paiements.

    Deux gardes, testées toutes les deux : le recalage UNITAIRE refuse,
    et le recalage GLOBAL ne visite même pas ces logements.
    """
    from app.services.locatif_depart import (
        recaler_statut_logement,
        recaler_tous_les_statuts_logements,
    )

    async def _seed() -> int:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name="Immeuble Externe Statut", address="100 rue Externe",
                city="Valcourt", is_active=True, gestion_externe=True,
            )
            s.add(imm)
            await s.flush()
            # Occupé À LA MAIN, AUCUN bail — la situation normale d'un
            # immeuble en gestion externe.
            lg = Logement(
                immeuble_id=imm.id, numero="957-2",
                status=LogementStatus.OCCUPE.value,
            )
            s.add(lg)
            await s.flush()
            await s.commit()
            return lg.id

    lg_id = run(_seed())

    async def _recaler_unitaire() -> str:
        async with TestSessionLocal() as s:
            await recaler_statut_logement(s, lg_id)
            await s.commit()
        async with TestSessionLocal() as s:
            return (await s.get(Logement, lg_id)).status

    assert run(_recaler_unitaire()) == LogementStatus.OCCUPE.value, (
        "recaler_statut_logement ne doit JAMAIS toucher la gestion externe"
    )

    async def _recaler_global() -> str:
        async with TestSessionLocal() as s:
            await recaler_tous_les_statuts_logements(s)
        async with TestSessionLocal() as s:
            return (await s.get(Logement, lg_id)).status

    assert run(_recaler_global()) == LogementStatus.OCCUPE.value, (
        "le recalage global ne doit pas visiter la gestion externe"
    )
