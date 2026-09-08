"""Smoke API — chantier stratégies d'acquisition, 4e ronde (retours Phil
2026-09-04).

Couvre le parcours complet par l'API (fiche → PATCH → moteur → PDF →
TRI) :
- cashback : le prix saisi est celui de l'acte, le cashback réduit
  le cash, total dépensé = coût réel (prix − cashback) + frais ;
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
            "cashback_montant": 20_000,
            "balance_vente_montant": 20_000,
            "balance_vente_taux_pct": 6.0,
            "projection_horizon_annees": 5,
        },
    )
    assert r.status_code == 200, r.text
    fiche = r.json()
    assert fiche["frais_demarrage_financables_json"] is None
    assert fiche["cashback_montant"] == 20_000
    res = json.loads(fiche["analysis_results_json"])
    t = res["traditionnel"]
    assert t is not None
    assert t["cashback"] == 20_000.0
    assert t["prix_reel"] == 980_000.0
    # BV 20 k ≤ mise de fonds restante (prix − prêt − cashback) → retenue.
    assert t["balance_vente"] == 20_000.0
    assert t["prix_achat"] == 1_000_000.0
    f = t["frais_demarrage"]
    assert f["frais_dossier_preteur"] == 5_000.0
    assert f["detention"] == 0.0
    assert abs(f["interets_balance_vente"] - 20_000 * 0.06 * 5) < 0.01
    for k in ("courtier_hypothecaire_2", "evaluateur_2", "notaire_2",
              "interets", "revenus_nets_pendant_projet"):
        assert f[k] == 0.0
    assert abs(f["courtier_hypothecaire_1"] - 0.01 * t["pret_retenu"]) < 0.01
    # Rien de coché → tout cash ; MDF nette = prix − prêt − cashback −
    # BV ; cash total = nette + frais ; total dépensé = coût réel + frais.
    assert t["frais_demarrage_cash"] == t["frais_demarrage_total"]
    attendu = (
        1_000_000 - t["pret_retenu"] - 20_000 - 20_000
    ) + t["frais_demarrage_cash"]
    assert abs(t["mdf_cash"] - attendu) < 0.01
    assert abs(t["total_depense"] - (980_000 + t["frais_demarrage_total"])) < 0.01
    det = t["detail_mdf_par_programme"][t["programme_retenu"]]
    assert abs(det["total_cash"] - t["mdf_cash"]) < 0.01
    # Projection : prêt max / écart / argent dégagé présents et
    # cohérents avec la colonne de référence à l'an H.
    p_h = next(p for p in t["projection"] if p["annee"] == t["horizon"])
    ref = t["refi"][t["best_refi"]["key"]]
    assert abs(p_h["pret_max"] - ref["financement"]) < 0.01
    assert abs(p_h["argent_degage"] - ref["equite_a_la_fin"]) < 0.01
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
        inp["rpv_achat"] - (t3["pret_retenu"] + 20_000) / 1_000_000
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
    # du projet, cashback toujours actif (déduit du cash).
    r = client.patch(
        base, headers=auth_headers,
        json={"strategie_acquisition": "preteur_b", "duree_projet_annees": 3},
    )
    assert r.status_code == 200, r.text
    fiche_b = r.json()
    assert fiche_b["frais_demarrage_financables_json"] is None
    res_b2 = json.loads(fiche_b["analysis_results_json"])
    assert res_b2["traditionnel"] is None
    assert res_b2["cashback"]["prix_reel"] == 980_000.0
    assert res_b2["pret_preteur_b"]["sur_prix"] == 750_000.0
    assert res_b2["mdf_pct_prix_achat"] == 250_000.0
    # Courtier 1 sur le prêt B (chantier actif).
    assert abs(
        res_b2["frais_demarrage"]["courtier_hypothecaire_1"] - 7_500
    ) < 0.01
    # Projection B : prêt max à l'an du refi = prêt de la référence.
    pb = res_b2["projection_preteur_b"]
    assert "pret_max" in pb[0] and "argent_degage" in pb[0]
    r = client.get(f"{base}/tri-inputs", headers=auth_headers)
    inp_b = r.json()["inputs"]
    assert inp_b["annee_refi"] == 3
    # En prêteur B la BV est plafonnée à l'assise restante après cashback
    # (250 k − 20 k ≥ 20 k → 20 k) : dette initiale = prêt B + BV.
    bv_b = res_b2["balance_vente"]["montant"]
    assert bv_b == 20_000.0
    assert abs(inp_b["rpv_achat"] - 0.77) < 1e-9
    # Pré-achat : bascule + recalcul, revenus d'achat = unités.
    r = client.patch(
        base, headers=auth_headers,
        json={
            "strategie_acquisition": "traditionnel",
            "optimisation_moment": "pre_achat",
            "unites_json": json.dumps(
                [{"typo": "3.5", "loyer_actuel": 833, "loyer_cible": 1200,
                  "optimiser": True}] * 10
            ),
        },
    )
    assert r.status_code == 200, r.text
    fiche_p = r.json()
    assert fiche_p["optimisation_moment"] == "pre_achat"
    tpa = json.loads(fiche_p["analysis_results_json"])["traditionnel"]
    assert tpa["optimisation_pre_achat"] is True
    assert abs(tpa["revenus_achat"] - 1200 * 10 * 12) < 0.01
    r = client.patch(base, headers=auth_headers, json={"optimisation_moment": "nimporte"})
    assert r.status_code == 422
    r = client.get(f"{base}/pdf", headers=auth_headers)
    assert r.status_code == 200, r.text


def test_parcours_residentiel(client, auth_headers, run):
    """Mode résidentiel par l'API : champs, moteur, carte kanban, PDF, TRI."""
    fid = _mk_fiche(run)
    base = f"/api/v1/lead-analyses/{fid}"
    r = client.patch(
        base, headers=auth_headers,
        json={
            "strategie_acquisition": "residentiel",
            "ltv_residentiel_pct": 75,
            "amort_residentiel_annees": 30,
            "depenses_residentiel_json": json.dumps(
                [{"label": "Gazon", "montant": 1500}]
            ),
            "depenses_optimisation_supp": 1000,
        },
    )
    assert r.status_code == 200, r.text
    r = client.post(f"{base}/run-financial-analysis", headers=auth_headers)
    assert r.status_code == 200, r.text
    out = r.json()
    res = out["analysis_results"]["residentiel"]
    assert res is not None
    assert res["pret_retenu"] == 750_000.0
    assert res["amort_annees"] == 30
    assert abs(res["depenses_reelles"] - (10_000 + 800 + 4_000 + 1_500)) < 0.01
    assert abs(res["depenses_optimisees"] - res["depenses_reelles"] - 1_000) < 0.01
    assert res["alerte_8_unites"] is True
    assert out["best_refi_program"].startswith("Résidentiel (prêt 75 %)")
    assert abs(float(out["best_refi_amount"]) - res["cashflow_optimise"]) < 0.01
    fiche = client.get(base, headers=auth_headers).json()
    assert abs(float(fiche["mdf_preteur_b"]) - res["mdf_cash"]) < 0.01
    assert fiche["ltv_residentiel_pct"] == 75
    # PDF reflet.
    r = client.get(f"{base}/pdf", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    # TRI : intrants du mode résidentiel.
    inp = client.get(f"{base}/tri-inputs", headers=auth_headers).json()["inputs"]
    assert abs(inp["rpv_achat"] - 0.75) < 1e-9
    assert abs(inp["mdf"] - res["mdf_cash"]) < 0.01
    assert inp["annee_refi"] == 5
    # Validation.
    r = client.patch(base, headers=auth_headers, json={"ltv_residentiel_pct": 150})
    assert r.status_code == 422
