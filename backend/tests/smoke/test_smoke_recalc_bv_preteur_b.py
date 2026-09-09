"""Smoke — retour Phil 2026-09-08 : « en prêteur B, quand je change la
balance de vente, la composition de la mise de fonds ne bouge pas ».

Reproduit le parcours réel : fiche avec unités détaillées, passage en
institution traditionnelle avec BV, puis retour en prêteur B et
changement de la BV — avec des Paramètres « comme en prod » (registre
des frais avec un poste masqué, poste personnalisé). Le recalcul auto
du PATCH doit mettre à jour la composition (résultats JSON) et, s'il
échoue, le dire dans la réponse (``recalc_error``) au lieu de se taire.
"""
from __future__ import annotations

import json

from sqlalchemy import delete

from app.models.prospection_analysis_default import ProspectionAnalysisDefault
from tests.smoke.conftest import TestSessionLocal
from tests.smoke.test_smoke_strategies_api import _mk_fiche


def _seed_defaults(run):
    async def _do():
        async with TestSessionLocal() as s:
            await s.execute(
                delete(ProspectionAnalysisDefault).where(
                    ProspectionAnalysisDefault.key.in_(
                        ["mdf_frais_registry", "frais_mdf_custom"]
                    )
                )
            )
            s.add(ProspectionAnalysisDefault(
                key="frais_mdf_custom",
                group="mdf_frais",
                label_fr="Postes perso",
                value_json=[{
                    "id": "custom_arpenteur", "label_fr": "Arpenteur",
                    "type_montant": "fixe", "valeur": 1200,
                    "financable_par_defaut": False,
                }],
            ))
            s.add(ProspectionAnalysisDefault(
                key="mdf_frais_registry",
                group="mdf_frais",
                label_fr="Registre",
                value_json=[
                    {"key": "courtier_hypothecaire_1", "label_fr": "Courtier 1", "visible": True},
                    {"key": "taxes_bienvenue", "label_fr": "Taxes de bienvenue", "visible": True},
                    {"key": "evaluateur", "label_fr": "Évaluateur", "visible": True},
                    {"key": "evaluateur_2", "label_fr": "Évaluateur 2", "visible": False},
                    {"key": "inspection", "label_fr": "Inspection", "visible": True},
                    {"key": "notaire", "label_fr": "Notaire", "visible": True},
                    {"key": "frais_travaux", "label_fr": "Travaux", "visible": True},
                    {"key": "interets", "label_fr": "Portage", "visible": True},
                    {"key": "custom_arpenteur", "label_fr": "Arpenteur", "visible": True},
                ],
            ))
            await s.commit()

    run(_do())


def _cleanup_defaults(run):
    async def _do():
        async with TestSessionLocal() as s:
            await s.execute(
                delete(ProspectionAnalysisDefault).where(
                    ProspectionAnalysisDefault.key.in_(
                        ["mdf_frais_registry", "frais_mdf_custom"]
                    )
                )
            )
            await s.commit()

    run(_do())


def test_bv_preteur_b_met_a_jour_la_composition(client, auth_headers, run):
    _seed_defaults(run)
    try:
        fid = _mk_fiche(run)
        base = f"/api/v1/lead-analyses/{fid}"
        unites = [
            {"typo": "3.5", "loyer_actuel": 800, "loyer_cible": 1200, "optimiser": i < 6}
            for i in range(10)
        ]
        r = client.patch(base, headers=auth_headers, json={
            "strategie_acquisition": "traditionnel",
            "unites_json": json.dumps(unites),
            "balance_vente_montant": 100_000,
            "balance_vente_taux_pct": 6,
            "optimisation_moment": "pre_achat",
        })
        assert r.status_code == 200, r.text
        r = client.post(f"{base}/run-financial-analysis", headers=auth_headers)
        assert r.status_code == 200, r.text

        # Retour en prêteur B puis changement de la BV : la composition
        # (résultats JSON) doit suivre à chaque PATCH, sans relancer.
        r = client.patch(base, headers=auth_headers, json={"strategie_acquisition": "preteur_b"})
        assert r.status_code == 200, r.text
        f1 = r.json()
        assert f1.get("recalc_error") in (None, "")
        res1 = json.loads(f1["analysis_results_json"])
        assert res1["traditionnel"] is None
        assert res1["balance_vente"]["montant"] == 100_000.0

        r = client.patch(base, headers=auth_headers, json={"balance_vente_montant": 150_000})
        assert r.status_code == 200, r.text
        f2 = r.json()
        assert f2.get("recalc_error") in (None, ""), f2.get("recalc_error")
        res2 = json.loads(f2["analysis_results_json"])
        assert res2["balance_vente"]["montant"] == 150_000.0
        assert abs((res1["mdf_preteur_b"] - res2["mdf_preteur_b"]) - (50_000 - 50_000 * 0.06 * 2)) < 0.01
        # Poste perso et poste masqué respectés.
        assert any(c["id"] == "custom_arpenteur" for c in res2["frais_demarrage"]["frais_custom"])
        assert res2["frais_demarrage"]["evaluateur_2"] == 0.0
        # BV au-delà de la mise de fonds (25 % × 1 M$ = 250 k) : retenue
        # telle quelle, le cash passe sous l'assise (l'excédent couvre les
        # frais) — plus de plafond silencieux.
        r = client.patch(base, headers=auth_headers, json={"balance_vente_montant": 400_000})
        assert r.status_code == 200, r.text
        res3 = json.loads(r.json()["analysis_results_json"])
        assert res3["balance_vente"]["montant"] == 400_000.0
        assert abs((res2["mdf_preteur_b"] - res3["mdf_preteur_b"]) - (250_000 - 250_000 * 0.06 * 2)) < 0.01
    finally:
        _cleanup_defaults(run)
