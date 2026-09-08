"""Smoke API — chantier stratégies d'acquisition, 4e ronde (retours Phil
2026-09-04).

Couvre le parcours complet par l'API (fiche → PATCH → moteur → PDF →
TRI) :
- cashback : prix déclaré, prêt plus gros, cash réduit, total dépensé
  = prix réel + frais ;
- composition traditionnelle : frais de dossier fixe, détention,
  intérêts BV provisionnés, postes prêteur B absents ;
- changement de stratégie → les coches « finançable » sont remises à
  zéro (rien en traditionnel, défauts en prêteur B) ;
- TRI : l'année du refi suit l'analyse (horizon en traditionnel, durée
  du projet en prêteur B) et les horizons sont n / n+5 / n+10 ;
- PDF : reflet (génération OK dans les deux modes).
"""
from __future__ import annotations

import json

from app.models.lead_analysis import LeadAnalysis

from tests.smoke.conftest import TestSessionLocal


def _mk_fiche(run) -> int:
    async def _create():
        async with TestSessionLocal() as s:
            rec = LeadAnalysis(
                address="456 rue Stratégie",
                city="Montréal",
                asking_price=1_000_000,
                nb_logements=10,
                typology_json=json.dumps({"3.5": 10}),
                revenus_bruts=100_000,
                taxes_municipales=10_000,
                taxes_scolaires=800,
                assurances=4_000,
                energie=0,
                depenses_autres=0,
                loyers_projetes_json=json.dumps({"3.5": 1200}),
                taux_interet_refi_pct=4.0,
                tga_pct=4.0,
                duree_projet_annees=2,
            )
            s.add(rec)
            await s.commit()
            await s.refresh(rec)
            return rec.id

    return run(_create())


def test_parcours_traditionnel_cashback_et_tri(client, auth_headers, run):
    fid = _mk_fiche(run)
    base = f"/api/v1/lead-analyses/{fid}"

    # Mode prêteur B d'abord, avec des coches finançables explicites.
    r = client.patch(
        base, headers=auth_headers,
        json={
            "strategie_acquisition": "preteur_b",
            "frais_demarrage_financables_json": json.dumps(["frais_travaux"]),
        },
    )
    assert r.status_code == 200, r.text
    r = client.post(f"{base}/run-financial-analysis", headers=auth_headers)
    assert r.status_code == 200, r.text
    res_b = r.json()["analysis_results"]
    assert res_b["traditionnel"] is None
    assert res_b["cashback"]["montant"] == 0.0
    # Postes permanents dans la liste exposée au front.
    assert "detention" in res_b["frais_demarrage"]

    # Bascule en institution traditionnelle + cashback + BV : les coches
    # finançables ne se transportent pas (reset → rien de coché).
    r = client.patch(
        base, headers=auth_headers,
        json={
            "strategie_acquisition": "traditionnel",
            "cashback_montant": 300_000,
            "balance_vente_montant": 100_000,
            "balance_vente_taux_pct": 6.0,
            "projection_horizon_annees": 5,
        },
    )
    assert r.status_code == 200, r.text
    fiche = r.json()
    assert fiche["frais_demarrage_financables_json"] is None
    assert fiche["cashback_montant"] == 300_000
    res = json.loads(fiche["analysis_results_json"])
    t = res["traditionnel"]
    assert t is not None
    assert t["cashback"] == 300_000.0
    assert t["prix_bancaire"] == 1_300_000.0
    f = t["frais_demarrage"]
    assert f["frais_dossier_preteur"] == 5_000.0
    assert f["detention"] == 0.0
    assert abs(f["interets_balance_vente"] - 100_000 * 0.06 * 5) < 0.01
    for k in ("courtier_hypothecaire_2", "evaluateur_2", "notaire_2",
              "interets", "revenus_nets_pendant_projet"):
        assert f[k] == 0.0
    assert abs(f["courtier_hypothecaire_1"] - 0.01 * t["pret_retenu"]) < 0.01
    # Rien de coché → tout cash ; MDF = prix déclaré − prêt − cashback −
    # BV + frais ; total dépensé = prix RÉEL + frais.
    assert t["frais_demarrage_cash"] == t["frais_demarrage_total"]
    attendu = max(
        0.0, 1_300_000 - t["pret_retenu"] - 300_000 - 100_000
    ) + t["frais_demarrage_cash"]
    assert abs(t["mdf_cash"] - attendu) < 0.01
    assert abs(t["total_depense"] - (1_000_000 + t["frais_demarrage_total"])) < 0.01
    # La carte kanban reflète le cash à l'achat du mode traditionnel.
    assert abs(float(fiche["mdf_preteur_b"]) - t["mdf_cash"]) < 0.01

    # Détention saisie sur la fiche → dans les frais et la MDF.
    r = client.patch(
        base, headers=auth_headers,
        json={"frais_demarrage_overrides_json": json.dumps({"detention": 20_000})},
    )
    assert r.status_code == 200, r.text
    t2 = json.loads(r.json()["analysis_results_json"])["traditionnel"]
    assert t2["frais_demarrage"]["detention"] == 20_000.0
    assert abs(t2["mdf_cash"] - t["mdf_cash"] - 20_000) < 0.01

    # Cocher un poste → roulé dans le prêt, 0 cash.
    r = client.patch(
        base, headers=auth_headers,
        json={"frais_demarrage_financables_json": json.dumps(["frais_dossier_preteur"])},
    )
    t3 = json.loads(r.json()["analysis_results_json"])["traditionnel"]
    assert abs(t3["frais_demarrage_total"] - t3["frais_demarrage_cash"] - 5_000) < 0.01

    # PDF : reflet, génération OK.
    r = client.get(f"{base}/pdf", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")

    # TRI : année du refi = horizon de détention ; intrants du mode trad.
    r = client.get(f"{base}/tri-inputs", headers=auth_headers)
    assert r.status_code == 200, r.text
    ti = r.json()
    assert ti["analysis_ready"] is True
    inp = ti["inputs"]
    assert inp["annee_refi"] == 5
    assert abs(inp["mdf"] - t3["mdf_cash"]) < 0.01
    assert abs(
        inp["rpv_achat"] - (t3["pret_retenu"] + 100_000) / 1_000_000
    ) < 1e-9
    ref = t3["refi"][t3["best_refi"]["key"]]
    assert abs(inp["valeur2"] - ref["valeur_retenue"]) < 0.01
    inp["capital"] = 500_000
    r = client.post(f"{base}/tri", headers=auth_headers, json=inp)
    assert r.status_code == 200, r.text
    tri = r.json()
    assert tri["horizons_list"] == [5, 10, 15]
    assert set(tri["tri"].keys()) == {"an5", "an10", "an15"}
    assert len(tri["flux"]["15"]) == 16

    # Retour en prêteur B : reset des coches (défauts), TRI à la durée
    # du projet, cashback toujours actif sur le prix déclaré.
    r = client.patch(
        base, headers=auth_headers,
        json={"strategie_acquisition": "preteur_b", "duree_projet_annees": 3},
    )
    assert r.status_code == 200, r.text
    fiche_b = r.json()
    assert fiche_b["frais_demarrage_financables_json"] is None
    res_b2 = json.loads(fiche_b["analysis_results_json"])
    assert res_b2["traditionnel"] is None
    assert res_b2["cashback"]["prix_bancaire"] == 1_300_000.0
    assert res_b2["pret_preteur_b"]["sur_prix"] == 0.75 * 1_300_000
    assert res_b2["mdf_pct_prix_achat"] == 0.25 * 1_300_000
    # Courtier 1 sur le prêt B (chantier actif).
    assert abs(
        res_b2["frais_demarrage"]["courtier_hypothecaire_1"] - 0.01 * 0.75 * 1_300_000
    ) < 0.01
    r = client.get(f"{base}/tri-inputs", headers=auth_headers)
    inp_b = r.json()["inputs"]
    assert inp_b["annee_refi"] == 3
    # En prêteur B la BV est plafonnée à l'assise restante après cashback
    # (325 k − 300 k = 25 k) : dette initiale = prêt B + BV retenue.
    bv_b = res_b2["balance_vente"]["montant"]
    assert abs(bv_b - 25_000) < 0.01
    assert abs(
        inp_b["rpv_achat"] - (0.75 * 1_300_000 + bv_b) / 1_000_000
    ) < 1e-9
    r = client.get(f"{base}/pdf", headers=auth_headers)
    assert r.status_code == 200, r.text
