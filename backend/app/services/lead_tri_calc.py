"""Moteur de calcul du TRI (taux de rendement interne) investisseur.

Reproduit — **au centime près** — le calculateur Excel validé qui projette
le rendement d'un investisseur minoritaire dans un deal immobilier de type
« achat — chantier/stabilisation — refinancement ».

Logique (cf. ``lead_tri_spec.md``, sections ①-⑤ + IRR) :

  ① Bases       : hypothèque d'achat, marge de manœuvre, RNO an 2,
                  multiplicateur de valeur (≈ inverse du cap rate).
  ② Projection  : loyers, dépenses, RNO, valeur immeuble aux horizons
                  an 2 / an 7 / an 12 (croissances composées).
  ③ Refi        : prêt maximal au refi (LTV × valeur), équité, argent
                  disponible à chaque horizon.
  ④ Cascade     : retour de capital prioritaire jusqu'à concurrence du
                  capital injecté, surplus partagé au prorata des parts.
  ⑤ Flux + TRI  : 3 lignes de temps de flux (sortie an 2 / an 7 / an 12),
                  chacune actualisée par bissection (``irr``).

Tous les calculs sont défensifs contre ``None`` et les divisions par zéro :
un intrant manquant est traité comme ``0.0`` et un dénominateur nul renvoie
``0.0`` plutôt que de lever une exception (le front pré-remplit 8 intrants
sur 12, l'utilisateur peut en laisser à blanc temporairement).
"""

from __future__ import annotations

from typing import Dict, List, Optional

# Horizons de sortie modélisés (en années depuis l'achat).
HORIZONS: List[int] = [2, 7, 12]

# Exposant de croissance composée appliqué à chaque horizon : l'an 2 est
# l'année « stabilisée » de référence (exposant 0), puis 5 ans jusqu'à
# l'an 7 et 10 ans jusqu'à l'an 12.
_EXPO: Dict[int, int] = {2: 0, 7: 5, 12: 10}


def _f(value: Optional[float]) -> float:
    """Coerce défensif vers ``float`` (``None`` → ``0.0``)."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def irr(
    cashflows: List[float],
    lo: float = -0.9999,
    hi: float = 10.0,
    tol: float = 1e-9,
    maxit: int = 200,
) -> Optional[float]:
    """Taux de rendement interne d'une suite de flux par bissection.

    ``cashflows[t]`` est le flux net à la période ``t`` (an 0, 1, 2, …).
    Retourne le taux ``r`` annuel tel que la VAN s'annule, ou ``None`` si
    aucun changement de signe n'est trouvé sur l'intervalle ``[lo, hi]``
    (cas dégénéré : flux tous de même signe, donc pas de racine réelle).

    Méthode robuste (pas de Newton) : on encadre la racine et on resserre
    l'intervalle de moitié à chaque itération. Reproduit exactement le
    calculateur de référence validé.
    """

    def npv(r: float) -> float:
        return sum(cf / (1.0 + r) ** t for t, cf in enumerate(cashflows))

    flo, fhi = npv(lo), npv(hi)
    # Pas de changement de signe sur l'intervalle → pas de racine encadrable.
    if flo * fhi > 0:
        return None
    for _ in range(maxit):
        mid = (lo + hi) / 2.0
        fm = npv(mid)
        if abs(fm) < tol:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2.0


def compute_tri(
    prix: Optional[float],
    rpv_achat: Optional[float],
    pret_constr: Optional[float],
    mdf: Optional[float],
    capital: Optional[float],
    pct: Optional[float],
    loyers2: Optional[float],
    dep2: Optional[float],
    valeur2: Optional[float],
    rpv_refi: Optional[float],
    cr_loyers: Optional[float],
    cr_dep: Optional[float],
) -> dict:
    """Calcule le TRI investisseur et toutes les métriques d'affichage.

    12 intrants (cf. spec) :

    - ``prix``       : prix d'achat de l'immeuble.
    - ``rpv_achat``  : ratio prêt-valeur à l'achat (ex. 0.8 = 80 %).
    - ``pret_constr``: prêt de construction / chantier (financé par le
      prêteur B en sus de l'hypothèque d'achat).
    - ``mdf``        : mise de fonds nécessaire (cash sorti à l'achat).
    - ``capital``    : capital total injecté par l'investisseur (base du TRI).
    - ``pct``        : fraction des parts détenues par l'investisseur
      (ex. 0.5 = 50 %).
    - ``loyers2``    : loyers bruts stabilisés de l'an 2.
    - ``dep2``       : dépenses d'opération de l'an 2.
    - ``valeur2``    : valeur de l'immeuble stabilisée an 2.
    - ``rpv_refi``   : ratio prêt-valeur au refinancement (ex. 0.85).
    - ``cr_loyers``  : croissance annuelle des loyers (ex. 0.03 = 3 %).
    - ``cr_dep``     : croissance annuelle des dépenses.

    Retourne un dict riche : bases, projection par horizon, sommaire,
    3 lignes de flux et les 3 TRI (sortie an 2 / an 7 / an 12).
    """
    # ── Normalisation défensive ──────────────────────────────────────
    prix = _f(prix)
    rpv_achat = _f(rpv_achat)
    pret_constr = _f(pret_constr)
    mdf = _f(mdf)
    capital = _f(capital)
    pct = _f(pct)
    loyers2 = _f(loyers2)
    dep2 = _f(dep2)
    valeur2 = _f(valeur2)
    rpv_refi = _f(rpv_refi)
    cr_loyers = _f(cr_loyers)
    cr_dep = _f(cr_dep)

    # ── ① Bases ──────────────────────────────────────────────────────
    hypotheque = rpv_achat * prix
    marge = capital - mdf
    rno2 = loyers2 - dep2
    # Multiplicateur de valeur = valeur / RNO (≈ inverse du cap rate).
    # Garde contre RNO nul : sans RNO la valeur économique n'a pas de
    # sens, on neutralise le multiplicateur (et donc les valeurs futures).
    multiplicateur = valeur2 / rno2 if rno2 != 0 else 0.0
    cap_rate = 1.0 / multiplicateur if multiplicateur != 0 else 0.0

    # ── ② Projection par horizon ─────────────────────────────────────
    loyers: Dict[int, float] = {}
    dep: Dict[int, float] = {}
    rno: Dict[int, float] = {}
    valeur: Dict[int, float] = {}
    pret_refi: Dict[int, float] = {}
    equite: Dict[int, float] = {}

    for h in HORIZONS:
        loyers[h] = loyers2 * (1 + cr_loyers) ** _EXPO[h]
        dep[h] = dep2 * (1 + cr_dep) ** _EXPO[h]
        rno[h] = loyers[h] - dep[h]

    # Valeur an 2 = valeur de référence ; an 7 / an 12 = RNO × multiplicateur.
    valeur[2] = valeur2
    valeur[7] = rno[7] * multiplicateur
    valeur[12] = rno[12] * multiplicateur

    # ── ③ Refi : prêt max, équité ────────────────────────────────────
    for h in HORIZONS:
        pret_refi[h] = rpv_refi * valeur[h]
        equite[h] = valeur[h] - pret_refi[h]

    # Argent disponible à chaque refinancement.
    dispo: Dict[int, float] = {}
    dispo[2] = pret_refi[2] + marge - (hypotheque + pret_constr)
    dispo[7] = pret_refi[7] - pret_refi[2]
    dispo[12] = pret_refi[12] - pret_refi[7]

    # ── ④ Cascade : retour de capital prioritaire + surplus partagé ──
    retour: Dict[int, float] = {}
    surplus: Dict[int, float] = {}
    restant_apres: Dict[int, float] = {}
    restant_avant: Dict[int, float] = {2: capital}

    for i, h in enumerate(HORIZONS):
        ra = restant_avant[h]
        # On rembourse le capital en priorité, jamais plus que le dispo ni
        # que le restant dû, jamais négatif.
        retour[h] = max(0.0, min(dispo[h], ra))
        surplus[h] = max(0.0, dispo[h] - retour[h])
        restant_apres[h] = ra - retour[h]
        if i + 1 < len(HORIZONS):
            restant_avant[HORIZONS[i + 1]] = restant_apres[h]

    # Cash effectivement encaissé par l'investisseur + valeur de ses parts.
    cash: Dict[int, float] = {}
    valeur_parts: Dict[int, float] = {}
    for h in HORIZONS:
        # Le retour de capital lui revient en propre ; il ne partage que le
        # surplus au prorata de ses parts.
        cash[h] = retour[h] + pct * surplus[h]
        valeur_parts[h] = pct * equite[h] + restant_apres[h]

    # ── ⑤ Flux + TRI par année de sortie ─────────────────────────────
    tri: Dict[int, Optional[float]] = {}
    flows_by_exit: Dict[int, List[float]] = {}
    for exit_year in HORIZONS:
        # Ligne de temps de 13 ans (index 0..12).
        f = [0.0] * 13
        f[0] = -capital
        f[2] += cash[2]
        if exit_year >= 7:
            f[7] += cash[7]
        if exit_year >= 12:
            f[12] += cash[12]
        # À la sortie, on liquide la valeur des parts détenues.
        f[exit_year] += valeur_parts[exit_year]
        flows_by_exit[exit_year] = f
        tri[exit_year] = irr(f)

    # ── Assemblage du dict riche ─────────────────────────────────────
    horizons_out = {
        str(h): {
            "loyers": loyers[h],
            "depenses": dep[h],
            "rno": rno[h],
            "valeur_immeuble": valeur[h],
            "pret_max_refi": pret_refi[h],
            "argent_dispo": dispo[h],
            "equite": equite[h],
            "retour_capital": retour[h],
            "surplus": surplus[h],
            "cash_investisseur": cash[h],
            "valeur_parts": valeur_parts[h],
        }
        for h in HORIZONS
    }

    # Total du cash encaissé sans tenir compte de la vente (somme des cash
    # intermédiaires aux 3 horizons) — utile pour juger le rendement courant.
    total_cash_sans_vente = cash[2] + cash[7] + cash[12]

    return {
        "intrants": {
            "prix": prix,
            "rpv_achat": rpv_achat,
            "pret_constr": pret_constr,
            "mdf": mdf,
            "capital": capital,
            "pct": pct,
            "loyers2": loyers2,
            "dep2": dep2,
            "valeur2": valeur2,
            "rpv_refi": rpv_refi,
            "cr_loyers": cr_loyers,
            "cr_dep": cr_dep,
        },
        "bases": {
            "hypotheque": hypotheque,
            "marge": marge,
            "rno2": rno2,
            "multiplicateur": multiplicateur,
            "cap_rate": cap_rate,
        },
        "horizons": horizons_out,
        "sommaire": {
            "mise_initiale": capital,
            "cash_an2": cash[2],
            "cash_an7": cash[7],
            "cash_an12": cash[12],
            "valeur_parts_an12": valeur_parts[12],
            "total_cash_sans_vente": total_cash_sans_vente,
        },
        "flux": {
            str(exit_year): flows_by_exit[exit_year] for exit_year in HORIZONS
        },
        "tri": {
            "an2": tri[2],
            "an7": tri[7],
            "an12": tri[12],
        },
    }
