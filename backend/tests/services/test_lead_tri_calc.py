"""Tests du moteur de calcul du TRI investisseur (lead_tri_calc).

Vérifie que `compute_tri` reproduit **au centième** les 3 deals de
référence validés sur le calculateur Excel d'origine. Tolérance :
±0,005 point de pourcentage sur chaque TRI (an 2 / an 7 / an 12).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.lead_tri_calc import compute_tri, irr


# Tolérance absolue sur le TRI (fraction). 0,00005 = 0,005 point de %.
TOL = 5e-5


# (label, intrants, (tri_an2, tri_an7, tri_an12) attendus)
SCENARIOS = [
    (
        "Deal 1",
        dict(
            prix=1150000, rpv_achat=0.8, pret_constr=602120, mdf=548695,
            capital=575000, pct=0.4, loyers2=171600, dep2=41807,
            valeur2=2886530, rpv_refi=0.85, cr_loyers=0.03, cr_dep=0.03,
        ),
        (0.25198, 0.22226, 0.21375),
    ),
    (
        "Deal 1(2)",
        dict(
            prix=1150000, rpv_achat=0.8, pret_constr=626120, mdf=554695,
            capital=575000, pct=0.5, loyers2=171600, dep2=41807,
            valeur2=2886530, rpv_refi=0.85, cr_loyers=0.03, cr_dep=0.03,
        ),
        (0.29739, 0.24960, 0.23730),
    ),
    (
        "Deal 2",
        dict(
            prix=1100000, rpv_achat=0.8, pret_constr=231720, mdf=435997,
            capital=450000, pct=0.5, loyers2=105600, dep2=27984,
            valeur2=1726140, rpv_refi=0.85, cr_loyers=0.05, cr_dep=0.03,
        ),
        (0.13476, 0.14770, 0.16534),
    ),
]


def test_scenarios_de_reference():
    """Les 3 deals validés matchent au centième sur les 3 horizons."""
    for label, intrants, (e2, e7, e12) in SCENARIOS:
        r = compute_tri(**intrants)
        got = (r["tri"]["an2"], r["tri"]["an7"], r["tri"]["an12"])
        for horizon, g, e in zip(("an2", "an7", "an12"), got, (e2, e7, e12)):
            assert g is not None, f"{label} {horizon}: TRI introuvable"
            assert abs(g - e) <= TOL, (
                f"{label} {horizon}: got={g:.5f} expected={e:.5f} "
                f"diff={abs(g - e):.6f}"
            )


def test_dict_riche_complet():
    """Le dict de sortie expose toutes les métriques d'affichage."""
    r = compute_tri(**SCENARIOS[0][1])

    # Bases
    bases = r["bases"]
    for k in ("hypotheque", "marge", "rno2", "multiplicateur", "cap_rate"):
        assert k in bases
    # hypotheque = rpv_achat × prix
    assert abs(bases["hypotheque"] - 0.8 * 1150000) < 0.01
    # cap_rate = 1 / multiplicateur
    assert abs(bases["cap_rate"] - 1.0 / bases["multiplicateur"]) < 1e-9

    # Horizons : toutes les métriques par horizon.
    for h in ("2", "7", "12"):
        hz = r["horizons"][h]
        for k in (
            "loyers", "depenses", "rno", "valeur_immeuble", "pret_max_refi",
            "argent_dispo", "equite", "retour_capital", "surplus",
            "cash_investisseur", "valeur_parts",
        ):
            assert k in hz, f"horizon {h} manque {k}"

    # Sommaire
    som = r["sommaire"]
    for k in (
        "mise_initiale", "cash_an2", "cash_an7", "cash_an12",
        "valeur_parts_an12", "total_cash_sans_vente",
    ):
        assert k in som

    # Flux : 3 lignes de 13 périodes, débutant par -capital.
    for exit_year in ("2", "7", "12"):
        f = r["flux"][exit_year]
        assert len(f) == 13
        assert abs(f[0] - (-575000)) < 0.01

    # TRI : 3 horizons.
    assert set(r["tri"].keys()) == {"an2", "an7", "an12"}


def test_irr_pas_de_racine_renvoie_none():
    """Flux tous positifs (ou tous négatifs) → pas de racine → None."""
    assert irr([100.0, 100.0, 100.0]) is None
    assert irr([-100.0, -100.0]) is None


def test_irr_cas_simple():
    """Flux -100 puis +110 à t=1 → TRI = 10 %."""
    r = irr([-100.0, 110.0])
    assert r is not None
    assert abs(r - 0.10) < 1e-6


def test_defensif_contre_none_et_zero():
    """Intrants None / zéro ne lèvent pas d'exception."""
    r = compute_tri(
        prix=None, rpv_achat=None, pret_constr=None, mdf=None,
        capital=None, pct=None, loyers2=None, dep2=None,
        valeur2=None, rpv_refi=None, cr_loyers=None, cr_dep=None,
    )
    # rno2 = 0 → multiplicateur neutralisé à 0, pas de division par zéro.
    assert r["bases"]["multiplicateur"] == 0.0
    assert r["bases"]["cap_rate"] == 0.0


if __name__ == "__main__":
    test_scenarios_de_reference()
    test_dict_riche_complet()
    test_irr_pas_de_racine_renvoie_none()
    test_irr_cas_simple()
    test_defensif_contre_none_et_zero()
    print("Tous les tests TRI passent.")
