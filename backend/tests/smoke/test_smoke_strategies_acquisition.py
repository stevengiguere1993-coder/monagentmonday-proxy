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
    # BV plafonnée à la mise de fonds restante (prix − prêt retenu).
    bv = min(100_000.0, max(0.0, 1_000_000 - t["pret_retenu"]))
    assert abs(t["balance_vente"] - bv) < 0.01
    assert abs(t["interets_bv_annuels"] - bv * 0.06) < 0.01
    dep = t["achat"]["conventionnel"]["depenses"]
    assert dep["interets_balance_vente"] == 0.0
    # Ligne propre : BV × 6 % × 5 ans.
    assert abs(
        t["frais_demarrage"]["interets_balance_vente"] - bv * 0.06 * 5
    ) < 0.01
    # La MDF du retenu déduit la BV ; frais tous cash (rien de coché).
    attendu = (1_000_000 - t["pret_retenu"] - bv) + t["frais_demarrage_cash"]
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
    """Cashback 100 k$ sur 1 M$ (retour Phil 2026-09-08 : le prix saisi
    EST celui vu par la banque) : le prêt B et les frais ne bougent
    pas, le cash de la MDF baisse de 100 k$ et le total dépensé = coût
    réel (900 k$) + frais."""
    sans = compute_all(_inputs(chantier_actif=True), use_aph_select=False)
    avec = compute_all(
        _inputs(chantier_actif=True, cashback_montant=100_000.0),
        use_aph_select=False,
    )
    d = avec.to_dict()
    assert d["cashback"] == {"montant": 100_000.0, "prix_reel": 900_000.0}
    assert avec.pret_preteur_b_sur_prix == 750_000.0
    assert d["mdf_pct_prix_achat"] == 250_000.0
    assert abs(avec.frais_demarrage.total - sans.frais_demarrage.total) < 0.01
    assert abs((sans.mdf_preteur_b - avec.mdf_preteur_b) - 100_000) < 0.01
    assert abs(
        avec.prix_acquisition - (900_000 + avec.frais_demarrage.total)
    ) < 0.01
    # Courtier 1 sur le prêt B (chantier) : 1 % × 750 k$.
    assert abs(avec.frais_demarrage.courtier_hypothecaire_1 - 7_500) < 0.01
    # Argent dégagé au refi = prêt − total dépensé (coût réel + frais).
    assert abs(
        (avec.refi_schl.equite_a_la_fin or 0)
        - (avec.refi_schl.financement - avec.prix_acquisition)
    ) < 0.01


def test_cashback_traditionnel():
    """Cashback en institution traditionnelle : le prêt ne bouge pas
    (la banque voit le prix saisi), la MDF nette = prix − prêt −
    cashback, total dépensé = coût réel + frais, et chaque colonne
    porte sa décomposition (brute / nette / frais / total)."""
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
    assert ta["prix_reel"] == 700_000.0
    assert ta["pret_retenu"] == ts["pret_retenu"]
    assert abs(ta["mdf_cash"] - (ts["mdf_cash"] - 300_000)) < 0.01
    assert abs(
        ta["total_depense"] - (700_000 + ta["frais_demarrage_total"])
    ) < 0.01
    for prog, sc in ta["achat"].items():
        d = ta["detail_mdf_par_programme"][prog]
        assert abs(d["mdf_brute"] - (1_000_000 - sc["financement"])) < 0.01
        assert d["cashback"] == 300_000.0
        assert abs(d["mdf_nette"] - (d["mdf_brute"] - 300_000)) < 0.01
        assert abs(d["total_cash"] - (d["mdf_nette"] + d["frais_cash"])) < 0.01
        assert abs(ta["mdf_par_programme"][prog] - d["total_cash"]) < 0.01
    # Cohérence haut/bas : à l'an H, prêt max de la projection = prêt
    # accordé de la référence et argent dégagé = celui de la colonne.
    ref = ta["refi"][ta["best_refi"]["key"]]
    p_h = next(p for p in ta["projection"] if p["annee"] == ta["horizon"])
    assert abs(p_h["pret_max"] - ref["financement"]) < 0.01
    assert abs(p_h["argent_degage"] - ref["equite_a_la_fin"]) < 0.01
    assert abs(p_h["ecart_pret"] - (p_h["pret_max"] - p_h["solde_pret"])) < 0.02


def test_optimisation_pre_achat():
    """Retour Phil 2026-09-08 : en pré-achat, l'achat se finance sur les
    loyers des unités (cible si optimisée, sinon actuel) et au refi
    ces loyers ont crû organiquement depuis l'an 0. Post-achat (défaut)
    = revenus de la fiche à l'achat, cibles au refi."""
    unites = _unites(6, 4)  # 6 × 1 200 $ + 4 × actuel
    actuel = 100_000 / 12 / 10
    rev_opt = (6 * 1_200.0 + 4 * actuel) * 12.0
    post = compute_all(
        _inputs(
            strategie="traditionnel", chantier_actif=True, unites=unites,
            projection_horizon_annees=5, croissance_loyers=0.03,
        ),
        use_aph_select=False,
    )
    pre = compute_all(
        _inputs(
            strategie="traditionnel", chantier_actif=True, unites=unites,
            projection_horizon_annees=5, croissance_loyers=0.03,
            optimisation_pre_achat=True,
        ),
        use_aph_select=False,
    )
    tp, tq = post.to_dict()["traditionnel"], pre.to_dict()["traditionnel"]
    assert tp["optimisation_pre_achat"] is False
    assert tq["optimisation_pre_achat"] is True
    # Achat : revenus de la fiche (post) vs somme des unités (pré).
    assert abs(tp["revenus_achat"] - 100_000) < 0.01
    assert abs(tq["revenus_achat"] - rev_opt) < 0.01
    assert abs(tp["achat"]["conventionnel"]["revenus_totaux"] - 100_000) < 0.01
    assert abs(tq["achat"]["conventionnel"]["revenus_totaux"] - rev_opt) < 0.01
    # Le prêt du jeu de test est plafonné au prix (1 M$) : la valeur
    # économique, elle, monte avec les loyers optimisés.
    assert (
        tq["achat"]["conventionnel"]["valeur_eco_rcd"]
        > tp["achat"]["conventionnel"]["valeur_eco_rcd"]
    )
    # Refi an 5 : pré = toutes les unités × 1,03^5 ; post = cibles ×
    # 1,03^4 + actuels × 1,03^5.
    attendu_pre = (6 * 1_200.0 + 4 * actuel) * 1.03 ** 5 * 12.0
    attendu_post = (6 * 1_200.0 * 1.03 ** 4 + 4 * actuel * 1.03 ** 5) * 12.0
    assert abs(tq["refi"]["conventionnel"]["revenus_totaux"] - attendu_pre) < 0.01
    assert abs(tp["refi"]["conventionnel"]["revenus_totaux"] - attendu_post) < 0.01
    assert abs(tq["projection"][0]["revenus"] - rev_opt) < 0.01

    # Mode prêteur B : pré-achat = cibles × croissance sur la durée.
    b_post = compute_all(
        _inputs(chantier_actif=True, unites=unites, croissance_loyers=0.03),
        use_aph_select=False,
    )
    b_pre = compute_all(
        _inputs(
            chantier_actif=True, unites=unites, croissance_loyers=0.03,
            optimisation_pre_achat=True,
        ),
        use_aph_select=False,
    )
    assert abs(
        b_post.refi_schl.revenus_totaux
        - (6 * 1_200.0 + 4 * actuel * 1.03 ** 2) * 12.0
    ) < 0.01
    assert abs(
        b_pre.refi_schl.revenus_totaux
        - (6 * 1_200.0 + 4 * actuel) * 1.03 ** 2 * 12.0
    ) < 0.01
    # Projection B : à l'an du refi, prêt max = prêt de la référence.
    pb = b_pre.to_dict()["projection_preteur_b"]
    refs = {
        s.config.label: s
        for s in (b_pre.refi_schl, b_pre.refi_aph_50, b_pre.refi_aph_100)
        if s is not None
    }
    ref_b = refs[b_pre.best_refi_program]
    assert abs(pb[0]["pret_max"] - ref_b.financement) < 0.01
    assert abs(
        pb[0]["argent_degage"] - (ref_b.financement - b_pre.prix_acquisition)
    ) < 0.01
    assert abs(pb[0]["ecart_pret"] - (pb[0]["pret_max"] - pb[0]["solde_pret"])) < 0.02
