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

    # Refi (définition Phil 2026-09-02) : argent dégagé = prêt max −
    # TOTAL DÉPENSÉ (prix + frais), et le best est le max.
    assert abs(
        t["total_depense"]
        - (1_000_000 + t["frais_demarrage_total"])
    ) < 0.01
    for v in t["refi"].values():
        assert abs(
            (v["financement"] - t["total_depense"])
            - v["equite_a_la_fin"]
        ) < 0.01
    best = t["best_refi"]
    assert abs(
        best["argent_dispo"]
        - max(v["equite_a_la_fin"] for v in t["refi"].values())
    ) < 0.01

    # Cohérence tableau haut/bas : à l'an H, la projection reprend les
    # dépenses de la colonne de référence.
    ref = t["refi"][t["best_refi"]["key"]]
    p_h = next(
        x for x in t["projection"] if x["annee"] == t["horizon"]
    )
    assert abs(p_h["depenses"] - ref["depenses_total"]) < 0.01
    assert abs(p_h["revenus"] - ref["revenus_totaux"]) < 0.01


def test_traditionnel_programme_auto_et_financables():
    """Sans choix explicite, le programme retenu = celui au PRÊT LE
    PLUS ÉLEVÉ ; un poste coché finançable en trad sort du cash."""
    r = compute_all(
        _inputs(strategie="traditionnel"), use_aph_select=False
    )
    t = r.to_dict()["traditionnel"]
    prets = {k: v["financement"] for k, v in t["achat"].items()}
    assert t["programme_retenu"] == max(prets, key=lambda k: prets[k])
    assert t["programme_retenu"] == t["programme_retenu_auto"]
    # Défaut : rien de finançable → cash = total.
    assert t["frais_demarrage_cash"] == t["frais_demarrage_total"]

    r2 = compute_all(
        _inputs(
            strategie="traditionnel",
            frais_demarrage_financables=["notaire"],
        ),
        use_aph_select=False,
    )
    t2 = r2.to_dict()["traditionnel"]
    attendu = t2["frais_demarrage_total"] - t2["frais_demarrage"]["notaire"]
    assert abs(t2["frais_demarrage_cash"] - attendu) < 0.01
    assert t2["mdf_cash"] < t["mdf_cash"]


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
    """En traditionnel (retour Phil 2026-09-04) : la BV réduit la MDF et
    ses intérêts sont PROVISIONNÉS pour H ans dans la composition
    (ligne propre, comme en prêteur B) — plus une dépense annuelle."""
    r = compute_all(
        _inputs(
            strategie="traditionnel",
            balance_vente_montant=100_000.0,
            balance_vente_taux_pct=0.06,
            projection_horizon_annees=5,
        ),
        use_aph_select=False,
    )
    t = r.to_dict()["traditionnel"]
    assert t["interets_bv_annuels"] == 6_000.0
    dep = t["achat"]["conventionnel"]["depenses"]
    assert dep["interets_balance_vente"] == 0.0
    # Ligne propre : 100 k × 6 % × 5 ans.
    assert abs(t["frais_demarrage"]["interets_balance_vente"] - 30_000) < 0.01
    # La MDF du retenu déduit la BV ; frais tous cash (rien de coché).
    attendu = max(
        0.0,
        1_000_000 - t["pret_retenu"] - 100_000,
    ) + t["frais_demarrage_cash"]
    assert abs(t["mdf_cash"] - attendu) < 0.01
    assert t["frais_demarrage_cash"] == t["frais_demarrage_total"]


def test_traditionnel_composition_des_frais():
    """Composition trad (retour Phil 2026-09-04) : pas de 2e courtier /
    évaluateur / notaire ni portage ni revenus pendant projet ; courtier
    1 = 1 % du PRÊT du programme ; frais de dossier = 5 000 $ fixe ;
    poste « Détention » à 0 par défaut, surchargeable."""
    r = compute_all(
        _inputs(strategie="traditionnel", programme_achat="conventionnel"),
        use_aph_select=False,
    )
    t = r.to_dict()["traditionnel"]
    f = t["frais_demarrage"]
    for k in (
        "courtier_hypothecaire_2", "evaluateur_2", "notaire_2",
        "interets", "revenus_nets_pendant_projet",
    ):
        assert f[k] == 0.0, k
    assert abs(f["courtier_hypothecaire_1"] - 0.01 * t["pret_retenu"]) < 0.01
    assert f["frais_dossier_preteur"] == 5_000.0
    assert f["detention"] == 0.0
    assert t["frais_dossier_trad"] == 5_000.0

    # Override de fiche : détention 25 000 $, frais de dossier 8 000 $.
    r2 = compute_all(
        _inputs(
            strategie="traditionnel",
            programme_achat="conventionnel",
            frais_demarrage_overrides={
                "detention": 25_000.0, "frais_dossier_preteur": 8_000.0
            },
        ),
        use_aph_select=False,
    )
    t2 = r2.to_dict()["traditionnel"]
    assert t2["frais_demarrage"]["detention"] == 25_000.0
    assert t2["frais_demarrage"]["frais_dossier_preteur"] == 8_000.0
    assert abs(
        t2["frais_demarrage_total"] - t["frais_demarrage_total"]
        - 25_000 - 3_000
    ) < 0.01

    # Défaut global (Paramètres) : frais de dossier trad 12 000 $.
    r3 = compute_all(
        _inputs(
            strategie="traditionnel",
            programme_achat="conventionnel",
            frais_fixes_overrides={"frais_dossier_trad": 12_000.0},
        ),
        use_aph_select=False,
    )
    assert r3.to_dict()["traditionnel"]["frais_demarrage"][
        "frais_dossier_preteur"
    ] == 12_000.0


def test_courtier_1_sur_le_pret_en_chantier():
    """Retour Phil 2026-09-04 : le courtier hypothécaire 1 se calcule
    sur le PRÊT, pas sur le prix. Prod (chantier inactif) = prix,
    intact au centime."""
    prod = compute_all(_inputs(), use_aph_select=False)
    assert prod.frais_demarrage.courtier_hypothecaire_1 == 0.01 * 1_000_000
    chantier = compute_all(
        _inputs(chantier_actif=True, mdf_preteur_b_pct=0.25),
        use_aph_select=False,
    )
    assert abs(
        chantier.frais_demarrage.courtier_hypothecaire_1 - 0.01 * 750_000
    ) < 0.01


def test_cashback_preteur_b():
    """Cashback 300 k$ sur 1 M$ : la banque voit 1,3 M$ → prêt B 975 k$
    (75 %), assise de MDF 325 k$ − 300 k$ reçus au notaire = 25 k$ ;
    total dépensé reste prix RÉEL + frais."""
    sans = compute_all(_inputs(chantier_actif=True), use_aph_select=False)
    avec = compute_all(
        _inputs(chantier_actif=True, cashback_montant=300_000.0),
        use_aph_select=False,
    )
    d = avec.to_dict()
    assert d["cashback"] == {"montant": 300_000.0, "prix_bancaire": 1_300_000.0}
    assert avec.pret_preteur_b_sur_prix == 975_000.0
    assert d["mdf_pct_prix_achat"] == 325_000.0
    # MDF = 25 k$ d'assise + frais cash (frais recalculés sur 1,3 M$).
    assert abs(
        avec.mdf_preteur_b - (25_000 + avec.frais_demarrage.total
                              - _frais_finances(avec))
    ) < 1.0
    # − 300 k$ reçus, + frais recalculés sur 1,3 M$ (taxes de bienvenue,
    # courtier, dossier, portage sur un prêt plus gros).
    assert avec.mdf_preteur_b < sans.mdf_preteur_b - 100_000
    assert avec.frais_demarrage.total > sans.frais_demarrage.total
    # Total dépensé (prix d'acquisition) = prix réel + frais.
    assert abs(
        avec.prix_acquisition - (1_000_000 + avec.frais_demarrage.total)
    ) < 0.01
    # Taxes de bienvenue et courtier sur le prix / prêt déclarés.
    assert avec.frais_demarrage.taxes_bienvenue > sans.frais_demarrage.taxes_bienvenue
    assert abs(avec.frais_demarrage.courtier_hypothecaire_1 - 9_750) < 0.01


def _frais_finances(r) -> float:
    """Portion des frais finançables prise par le prêteur B (défaut :
    rapport efficacité / développement / travaux, à 75 %)."""
    return r.pret_preteur_b_frais_finances


def test_cashback_traditionnel():
    """Cashback en institution traditionnelle : la valeur marchande
    plafond passe à 1,3 M$ (prêt plus gros), la MDF déduit le cashback,
    total dépensé = prix réel + frais."""
    sans = compute_all(
        _inputs(strategie="traditionnel", programme_achat="conventionnel"),
        use_aph_select=False,
    )
    avec = compute_all(
        _inputs(
            strategie="traditionnel",
            programme_achat="conventionnel",
            cashback_montant=300_000.0,
        ),
        use_aph_select=False,
    )
    ts, ta = sans.to_dict()["traditionnel"], avec.to_dict()["traditionnel"]
    assert ta["cashback"] == 300_000.0
    assert ta["prix_bancaire"] == 1_300_000.0
    assert ta["pret_retenu"] >= ts["pret_retenu"]
    attendu = max(
        0.0, 1_300_000 - ta["pret_retenu"] - 300_000
    ) + ta["frais_demarrage_cash"]
    assert abs(ta["mdf_cash"] - attendu) < 0.01
    assert abs(
        ta["total_depense"] - (1_000_000 + ta["frais_demarrage_total"])
    ) < 0.01
    # Chaque colonne d'achat porte sa propre MDF (courtier sur SON prêt).
    for prog, sc in ta["achat"].items():
        att = max(
            0.0, 1_300_000 - sc["financement"] - 300_000
        )
        assert ta["mdf_par_programme"][prog] >= att - 0.01


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
