"""Smoke — suppression d'un immeuble : l'historique financier décide.

Retour Phil (2026-08-20) : « je peux pas supprimer un immeuble que
j'avais en test — encore le même bug ». L'ancienne garde comptait les
BAUX : un immeuble de test (baux, zéro paiement) était insupprimable,
et le chemin manuel menait de 409 en 409 — supprimer le bail actif
exigeait de passer par le dossier, supprimer le dossier exigeait que le
bail ne soit pas actif.

Ce qu'on protège, c'est l'HISTORIQUE FINANCIER. La garde est donc
recentrée : le moindre paiement, frais ou dépôt détenu bloque ; sinon la
suppression emporte proprement baux, dossiers, documents — et les
locataires créés pour cet immeuble, mais jamais ceux qui ont un bail
ailleurs.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.models.immobilier import (
    Bail,
    BailStatus,
    ImmDocument,
    Immeuble,
    Locataire,
    LocationDossier,
    LocationDossierStatut,
    Logement,
    LogementStatus,
    PaiementLoyer,
)

from .conftest import TestSessionLocal


def _seed(nom, *, avec_paiement=False, locataire_ailleurs=False):
    async def _s() -> dict:
        async with TestSessionLocal() as s:
            imm = Immeuble(
                name=nom, address=f"{nom} rue Test", city="Boucherville",
                is_active=True,
            )
            s.add(imm)
            await s.flush()
            lg = Logement(
                immeuble_id=imm.id, numero="101",
                status=LogementStatus.OCCUPE.value,
            )
            lo = Locataire(full_name=f"Testeur {nom}")
            s.add_all([lg, lo])
            await s.flush()
            b = Bail(
                logement_id=lg.id, locataire_id=lo.id,
                date_debut=date.today() - timedelta(days=30),
                date_fin=date.today() + timedelta(days=335),
                loyer_mensuel=5000.0, status=BailStatus.ACTIF.value,
            )
            s.add(b)
            await s.flush()
            s.add(
                LocationDossier(
                    logement_id=lg.id,
                    statut=LocationDossierStatut.BAIL_ENVOYE.value,
                    nouveau_bail_id=b.id,
                )
            )
            s.add(
                ImmDocument(
                    bail_id=b.id, locataire_id=lo.id, immeuble_id=imm.id,
                    type="consentement_communications",
                    titre="Consentement (test)",
                )
            )
            if avec_paiement:
                s.add(
                    PaiementLoyer(
                        bail_id=b.id,
                        mois_couvert=date.today().replace(day=1),
                        montant=5000.0,
                        created_at=datetime.now(timezone.utc),
                    )
                )
            autre_bail = None
            if locataire_ailleurs:
                imm2 = Immeuble(
                    name=f"{nom} Autre", address="2 rue Ailleurs",
                    city="Montréal", is_active=True,
                )
                s.add(imm2)
                await s.flush()
                lg2 = Logement(immeuble_id=imm2.id, numero="1")
                s.add(lg2)
                await s.flush()
                autre_bail = Bail(
                    logement_id=lg2.id, locataire_id=lo.id,
                    date_debut=date.today() - timedelta(days=400),
                    date_fin=date.today() - timedelta(days=100),
                    loyer_mensuel=900.0, status=BailStatus.TERMINE.value,
                )
                s.add(autre_bail)
                await s.flush()
            await s.commit()
            return {
                "immeuble_id": imm.id, "locataire_id": lo.id,
                "bail_id": b.id, "logement_id": lg.id,
            }

    return _s


def test_immeuble_de_test_se_supprime_au_complet(client, auth_headers, run):
    """Le cas de Phil : baux, dossier, documents — mais ZÉRO paiement.
    La suppression passe et emporte tout, y compris le locataire créé
    pour ce test."""
    ids = run(_seed("Test Supprimable")())
    r = client.delete(
        f"/api/v1/immobilier/immeubles/{ids['immeuble_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 204, r.text

    async def _restes() -> dict:
        async with TestSessionLocal() as s:
            return {
                "immeuble": await s.get(Immeuble, ids["immeuble_id"]),
                "bail": await s.get(Bail, ids["bail_id"]),
                "locataire": await s.get(Locataire, ids["locataire_id"]),
                "dossiers": (
                    await s.execute(
                        select(LocationDossier).where(
                            LocationDossier.logement_id
                            == ids["logement_id"]
                        )
                    )
                ).scalars().all(),
            }

    restes = run(_restes())
    assert restes["immeuble"] is None
    assert restes["bail"] is None
    assert restes["locataire"] is None, (
        "le locataire créé pour le test part avec l'immeuble"
    )
    assert restes["dossiers"] == []


def test_le_moindre_paiement_bloque(client, auth_headers, run):
    """Un seul paiement = un historique financier = 409. C'est la raison
    d'être de la garde, inchangée."""
    ids = run(_seed("Test Bloque", avec_paiement=True)())
    r = client.delete(
        f"/api/v1/immobilier/immeubles/{ids['immeuble_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 409, r.text
    assert "historique financier" in r.text


def test_un_locataire_avec_bail_ailleurs_survit(client, auth_headers, run):
    """Le locataire du test a AUSSI un bail dans un autre immeuble : la
    suppression emporte l'immeuble de test mais pas lui — on ne rase
    jamais quelqu'un qui existe ailleurs."""
    ids = run(_seed("Test Partage", locataire_ailleurs=True)())
    r = client.delete(
        f"/api/v1/immobilier/immeubles/{ids['immeuble_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 204, r.text

    async def _locataire():
        async with TestSessionLocal() as s:
            return await s.get(Locataire, ids["locataire_id"])

    assert run(_locataire()) is not None
