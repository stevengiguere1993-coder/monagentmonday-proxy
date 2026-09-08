"""Smoke — chantier « stratégies d'acquisition » (v2, retours Phil
2026-09-02).

Règles couvertes :
- rétrocompatibilité au centime quand la fiche n'a pas de stratégie ;
- balance de vente = réduit la MISE DE FONDS (pas le prêt B), avec le
  poste PERMANENT « intérêts balance de vente » ;
- croissance organique : unités non optimisées indexées sur la durée
  du projet, dépenses réelles indexées au refi (chantier seulement) ;
- stratégie « institution traditionnelle » : 4 programmes sur la même
  page (achat + refi an H), programme retenu, projection ;
- projection long terme du mode prêteur B.
"""
from __future__ import annotations

from app.services.lead_analysis_finance import FinanceInputs, compute_all


def _inputs(**kw) -> FinanceInputs:
    base = dict(
        adresse="123 rue Test",
        prix_achat=1_000_000.0,
        nombre_logements=10,
        revenus_annuels=100_000.0,
        taxes_municipales=10_000.0,
        taxes_scolaires=800.0,
        assurances=4_000.0,
        energie=0.0,
        depenses_autres=0.0,
        tga=0.04,
        taux_interet_achat=0.04,
        taux_interet_refi=0.04,
        typologie={"3.5": 10},
        typologie_prix={"3.5": 1_200.0},
        duree_projet_annees=2,
    )
    base.update(kw)
    return FinanceInputs(**base)


def _unites(n_opt: int, n_non: int) -> list[dict]:
    actuel = 100_000 / 12 / 10  # loyer actuel uniforme du jeu de test
    return (
        [
            {"typo": "3.5", "loyer_actuel": actuel,
             "loyer_cible": 1_200.0, "optimiser": True}
        ] * n_opt
        + [
            {"typo": "3.5", "loyer_actuel": actuel,
             "loyer_cible": 1_200.0, "optimiser": False}
        ] * n_non
    )


def test_defaut_retrocompatible():
    """Sans stratégie choisie : mêmes chiffres qu'avant, au centime."""
    r = compute_all(_inputs(), use_aph_select=False)
    assert r.frais_demarrage.interets == 0.75 * 1_000_000 * 0.08 * 2
    assert r.frais_demarrage.interets_balance_vente == 0.0
    # Dépenses refi NON indexées (chantier inactif).
    assert r.refi_schl.depenses.taxes_municipales == 10_000.0

    d = r.to_dict()
    assert d["strategie"] == "preteur_b"
    assert d["traditionnel"] is None
    assert d["projection_preteur_b"] is None
    assert d["pret_preteur_b"]["sur_prix"] == 750_000.0


def test_balance_vente_reduit_la_mdf():
    """BV 200 k$ @ 6 % : le prêt B ne bouge PAS, la mise de fonds cash
    baisse de 200 k$ (moins le nouveau poste d'intérêts BV, payé
    cash), et le poste permanent apparaît."""
    sans = compute_all(_inputs(), use_aph_select=False)
    avec = compute_all(
        _inputs(balance_vente_montant=200_000.0, balance_vente_taux_pct=0.06),
        use_aph_select=False,
    )
    # Prêt B et intérêts de portage inchangés.
    assert avec.pret_preteur_b_sur_prix == 750_000.0
    assert avec.frais_demarrage.interets == sans.frais_demarrage.interets
    # Poste permanent : 200k × 6 % × 2 ans.
    assert abs(
        avec.frais_demarrage.interets_balance_vente - 24_000
    ) < 0.01
    assert avec.balance_vente_retenue == 200_000.0
    # MDF : − 200 000 $ de BV + 24 000 $ d'intérêts BV (cash).
    assert abs(
        (sans.mdf_preteur_b - avec.mdf_preteur_b) - (200_000 - 24_000)
    ) < 0.01


def test_balance_vente_plafonnee_a_la_mdf():
    """La BV remplace du CASH : jamais plus que X % × prix."""
    r = compute_all(
        _inputs(balance_vente_montant=2_000_000.0),
        use_aph_select=False,
    )
    assert r.balance_vente_retenue == 250_000.0  # 25 % × 1 M$


def test_croissance_organique_mode_preteur_b():
    """Chantier actif : une unité NON optimisée = actuel × (1+3 %)²
    (durée du projet), et les dépenses réelles du refi sont indexées
    ×(1,03)²."""
    actuel = 100_000 / 12 / 10
    r = compute_all(
        _inputs(
            chantier_actif=True,
            unites=_unites(6, 4),
            croissance_loyers=0.03,
            croissance_depenses=0.03,
        ),
        use_aph_select=False,
    )
    attendu = (6 * 1_200.0 + 4 * actuel * 1.03**2) * 12.0
    assert abs(r.refi_schl.revenus_totaux - attendu) < 0.01
    assert abs(
        r.refi_schl.depenses.taxes_municipales - 10_000 * 1.03**2
    ) < 0.01
    # Projection long terme du mode B présente (années dès le refi).
    d = r.to_dict()
    assert d["projection_preteur_b"] is not None
    assert d["projection_preteur_b"][0]["annee"] == 2

    # SANS chantier : unités non optimisées gelées, dépenses intactes.
    r0 = compute_all(_inputs(unites=_unites(6, 4)), use_aph_select=False)
    attendu0 = (6 * 1_200.0 + 4 * actuel) * 12.0
    assert abs(r0.refi_schl.revenus_totaux - attendu0) < 0.01


def test_traditionnel_conventionnel():
    """Stratégie traditionnelle : 4 programmes à l'achat ET au refi,
    programme retenu → MDF, verdict cohérent."""
    r = compute_all(
        _inputs(
            strategie="traditionnel",
            programme_achat="conventionnel",
            chantier_actif=True,
            projection_horizon_annees=5,
            croissance_loyers=0.03,
            croissance_depenses=0.03,
        ),
        use_aph_select=False,
    )
    t = r.to_dict()["traditionnel"]
    assert t is not None
    assert t["programme_retenu"] == "conventionnel"
    assert set(t["achat"].keys()) == {
        "conventionnel", "schl_std", "aph_50", "aph_100"
    }
    assert set(t["refi"].keys()) == set(t["achat"].keys())

    # Frais : pas de phase chantier, pas de rapport d'efficacité en
    # conventionnel.
    f = t["frais_demarrage"]
    for k in (
        "courtier_hypothecaire_2", "evaluateur_2", "notaire_2",
        "interets", "revenus_nets_pendant_projet", "rapport_efficacite",
        "interets_balance_vente",
    ):
        assert f[k] == 0.0, k
    assert f["taxes_bienvenue"] > 0

    # MDF du retenu = prix − prêt + frais (pas de BV ici).
    attendu_mdf = (
        1_000_000 - t["pret_retenu"] + t["frais_demarrage_total"]
    )
    assert abs(t["mdf_cash"] - attendu_mdf) < 0.01

    # Projection : an 0 = réels ; an 1 = +3 %.
    p0, p1 = t["projection"][0], t["projection"][1]
    assert p0["revenus"] == 100_000.0
    assert abs(p1["revenus"] - 103_000.0) < 0.01

    # Refi : argent dégagé = prêt max − solde retenu − BV, et le best
    # est le max.
    for v in t["refi"].values():
        assert abs(
            (v["financement"] - t["solde_retenu_an_h"])
            - v["equite_a_la_fin"]
        ) < 0.01
    best = t["best_refi"]
    assert abs(
        best["argent_dispo"]
        - max(v["equite_a_la_fin"] for v in t["refi"].values())
    ) < 0.01


def test_traditionnel_alias_et_aph():
    """Les anciennes stratégies « achat direct » sont des alias ; un
    programme APH retenu garde le rapport d'efficacité."""
    r = compute_all(_inputs(strategie="aph_50"), use_aph_select=False)
    t = r.to_dict()["traditionnel"]
    assert t is not None
    assert t["programme_retenu"] == "aph_50"
    assert t["frais_demarrage"]["rapport_efficacite"] > 0
    assert t["achat"]["aph_50"]["ltv"] == 0.85


def test_traditionnel_balance_vente_et_depenses():
    """En traditionnel : la BV réduit la MDF et ses intérêts annuels
    apparaissent comme DÉPENSE permanente des colonnes d'achat."""
    r = compute_all(
        _inputs(
            strategie="traditionnel",
            balance_vente_montant=100_000.0,
            balance_vente_taux_pct=0.06,
        ),
        use_aph_select=False,
    )
    t = r.to_dict()["traditionnel"]
    assert t["interets_bv_annuels"] == 6_000.0
    dep = t["achat"]["conventionnel"]["depenses"]
    assert dep["interets_balance_vente"] == 6_000.0
    # La MDF du retenu déduit la BV.
    attendu = max(
        0.0,
        1_000_000 - t["pret_retenu"] - 100_000,
    ) + t["frais_demarrage_total"]
    assert abs(t["mdf_cash"] - attendu) < 0.01


def test_refi_reference_manuelle():
    """La référence choisie (refi_retenu) pilote le verdict et la
    carte, même si un autre programme est « meilleur »."""
    # Mode B : forcer SCHL standard comme référence.
    r = compute_all(
        _inputs(chantier_actif=True, refi_retenu="refi_schl"),
        use_aph_select=False,
    )
    assert r.best_refi_program == "SCHL standard"
    assert abs(
        (r.best_refi_amount or 0)
        - (r.refi_schl.equite_a_la_fin or 0)
    ) < 0.01

    # Traditionnel : référence schl_std ; le meilleur automatique reste
    # exposé séparément (étoile).
    r2 = compute_all(
        _inputs(strategie="traditionnel", refi_retenu="schl_std"),
        use_aph_select=False,
    )
    t = r2.to_dict()["traditionnel"]
    assert t["best_refi"]["key"] == "schl_std"
    assert t["meilleur_refi_key"] in t["refi"]


def test_unites_mode_traditionnel():
    """Refi an H : unité optimisée = cible dès l'an 1 puis croît ; non
    optimisée = actuel × (1+cl)^a."""
    actuel = 100_000 / 12 / 10
    r = compute_all(
        _inputs(
            strategie="traditionnel",
            chantier_actif=True,
            unites=_unites(6, 4),
            projection_horizon_annees=5,
            croissance_loyers=0.03,
            croissance_depenses=0.03,
        ),
        use_aph_select=False,
    )
    t = r.to_dict()["traditionnel"]
    p1 = t["projection"][1]
    attendu_1 = (6 * 1_200.0 + 4 * actuel * 1.03) * 12.0
    assert abs(p1["revenus"] - attendu_1) < 0.01
