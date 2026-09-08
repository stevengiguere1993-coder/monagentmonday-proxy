"""Moteur d'analyse financière pour les leads (Phase 3 de l'analyse).

Réplique **exactement** la mécanique des 2 calculateurs Excel :
  - `CALCULATEUR_OFFICIEL.xlsm` (col D = SCHL Efficacité énergétique 50 pts)
  - `CALCULATEUR_OFFICIEL_APH_SELECT.xlsm` (col D = SCHL Abord + Eff 100 pts)

La spec complète est dans `lead_analysis_spec.md`. Tests unitaires
dans `tests/services/test_lead_analysis_finance.py` (cas Saint-Joseph).

Architecture :
    inputs (dict)
        ↓
    compute_typology_aggregates → H13, abord/PDM split (APH SELECT)
        ↓
    compute_each_scenario(achat, schl, aph_50, aph_100) → revenus, dépenses,
                                                          valeur éco, financement
        ↓
    compute_frais_demarrage → L4..L19 → B5
        ↓
    compute_final_outcomes → MDF, équité, best_refi
        ↓
    {full_results, best_refi_amount, best_refi_program}
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ─── Barème des dépenses normalisées (K35..K44 dans l'Excel) ──────

BAREME: Dict[str, float] = {
    "concierge_lt12":  215.0,
    "concierge_gte12": 365.0,
    "entretien":       610.0,
    "gestion_lt12":    0.0425,
    "gestion_gte12":   0.05,
    "wifi_par_log":    5.0,
    "internet_fixe":   120.0,
    "thermopompe":     190.0,
    # Août 2026 — « Autres normalisations » : % des revenus bruts
    # (défaut 1 %), paramétrable comme le reste du barème.
    "autres_normalisations_pct": 0.01,
}

# Seuil de bascule du barème des dépenses normalisées (concierge +
# gestion). En-dessous de ce nombre de logements on applique les tarifs
# « petit immeuble » (concierge_lt12, gestion_lt12), au-dessus ou égal
# les tarifs « grand immeuble ». Cf. R39/R41 dans l'Excel.
#
# Juin 2026 : externalisé via le défaut global ``seuil_bascule_bareme_log``
# (groupe ``depenses_normalisees``). Le moteur lit la config si présente,
# sinon retombe sur cette constante (fallback ultime).
SEUIL_BASCULE_BAREME_LOG: int = 12


# ─── Barèmes fiscaux ───────────────────────────────────────────────
#
# Juin 2026 : externalisés via le groupe BD ``baremes_fiscaux``. Le
# moteur lit la config si présente, sinon retombe sur ces constantes.

# Ratio d'abordabilité APH SELECT : proportion des logements qui doivent
# être abordables (nb_abordables = ceil(ratio × nb_total)). Défaut 0.40
# (40 %). Externalisé via le défaut global ``ratio_abordabilite_aph``.
RATIO_ABORDABILITE_APH: float = 0.40

# Barème progressif des taxes de bienvenue de Montréal (2024-2025).
# Liste de tuples ``(seuil_haut, taux_fraction)`` ; le dernier palier
# (``math.inf``) couvre tout au-dessus du dernier seuil. Externalisé via
# le défaut global ``taxes_bienvenue_mtl`` (stocké en ``value_json`` sous
# forme ``[{"seuil": .., "taux_pct": ..}]``, ``seuil`` null = palier
# ouvert). Cf. L6 dans l'Excel.
TAXES_BIENVENUE_MTL_BRACKETS: List[tuple] = [
    (61_500,    0.005),
    (307_800,   0.010),
    (552_300,   0.015),
    (1_104_700, 0.020),
    (2_136_500, 0.025),
    (3_113_000, 0.035),
    (math.inf,  0.040),
]


# ─── Frais fixes (L7..L13) ─────────────────────────────────────────
#
# Valeurs par défaut « historiques » (hardcoded). Depuis la PR
# « extend-analysis-defaults-tous-champs » (mai 2026), ces frais sont
# overridables globalement via la table ``prospection_analysis_defaults``
# (groupe ``mdf_frais``). Les clés en BD sont préfixées ``frais_*`` pour
# ne pas entrer en collision avec d'autres défauts numériques. Le
# pourcentage des courtiers hypothécaires (1 %) est aussi rendu
# paramétrable (clés ``pct_courtier_hypothecaire_1`` / ``_2``).
#
# Override de hiérarchie :
#   1. Si l'utilisateur a saisi un override par fiche dans
#      ``frais_demarrage_overrides_json`` → cette valeur GAGNE.
#   2. Sinon, si un défaut global est présent en BD → on l'utilise.
#   3. Sinon, on retombe sur les constantes ``FRAIS_FIXES`` ci-dessous.

FRAIS_FIXES: Dict[str, float] = {
    "evaluateur":         1500.0,
    "evaluateur_2":       1500.0,
    "inspection":         1700.0,
    "avocat":             4000.0,
    "notaire":            1600.0,
    "notaire_2":          1600.0,
    "rapport_efficacite": 4500.0,
}

# Pourcentages des courtiers hypothécaires (L4 et L5 dans l'Excel).
# L4 = pct_courtier_hypothecaire_1 × prix d'achat.
# L5 = pct_courtier_hypothecaire_2 × financement APH 100 pts.
PCT_COURTIERS: Dict[str, float] = {
    "courtier_hypothecaire_1": 0.01,
    "courtier_hypothecaire_2": 0.01,
}

# Pourcentage des frais de dossier du prêteur B (mai 2026). Appliqué
# au prêt initial du prêteur B (= prix_achat × ltv_achat = 75 % du
# prix d'achat). Stocké en fraction (0.02 = 2 %). Pré-rempli depuis
# le défaut global ``frais_dossier_preteur_pct``.
DEFAULT_FRAIS_DOSSIER_PRETEUR_PCT: float = 0.02


# ─── Frais de démarrage PERSONNALISÉS (dynamiques, juin 2026) ──────
#
# En plus des postes FIXES ci-dessus (évaluateur, inspection, avocat,
# notaire 1/2, rapport efficacité, courtiers, frais dossier prêteur),
# l'admin peut AJOUTER/RETIRER des postes de frais de démarrage
# arbitraires depuis l'app. Ces postes sont stockés en BD dans le
# défaut global ``frais_mdf_custom`` (groupe ``mdf_frais``, dans
# ``value_json``) sous forme d'une LISTE d'objets :
#
#   {
#     "id":                  "<slug/uuid stable>",
#     "label_fr":            "<nom affiché>",
#     "type_montant":        "fixe" | "pct_prix_achat" | "pct_financement",
#     "valeur":              <montant $ OU taux selon le type>,
#     "financable_par_defaut": <bool>,
#   }
#
# Calcul du montant ($) selon ``type_montant`` :
#   - "fixe"           : ``valeur`` est un montant $ direct.
#   - "pct_prix_achat" : ``valeur`` est un % (5.0 = 5 %) appliqué au
#                        prix d'achat.
#   - "pct_financement": ``valeur`` est un % appliqué au financement
#                        refi retenu (best APH, comme le courtier 2).
#
# IMPORTANT — exactitude : tant qu'AUCUN poste personnalisé n'est
# créé (liste vide), le résultat du moteur est STRICTEMENT identique à
# avant. Les postes personnalisés ne s'ajoutent au calcul que s'ils
# existent.
FRAIS_CUSTOM_TYPES: tuple[str, ...] = (
    "fixe",
    "pct_prix_achat",
    "pct_financement",
)


def compute_frais_custom_montant(
    item: dict,
    prix_achat: float,
    financement_refi: float,
) -> float:
    """Calcule le montant $ d'un poste de frais personnalisé selon son
    ``type_montant``.

    ``item`` : dict ``{id, label_fr, type_montant, valeur,
    financable_par_defaut}``. ``financement_refi`` = financement du
    « best APH » retenu (même base que le courtier hypothécaire 2).

    Les pourcentages sont stockés en pourcentage (5.0 = 5 %) et
    convertis en fraction (÷100) ici. Type inconnu / valeur invalide
    → 0.0 (poste neutre, n'altère pas le calcul)."""
    try:
        valeur = float(item.get("valeur", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    type_montant = item.get("type_montant", "fixe")
    if type_montant == "fixe":
        return valeur
    if type_montant == "pct_prix_achat":
        return (valeur / 100.0) * float(prix_achat or 0)
    if type_montant == "pct_financement":
        return (valeur / 100.0) * float(financement_refi or 0)
    return 0.0

# LTV du prêt à l'achat conventionnel (Prêteur B) — utilisé pour
# calculer la base des frais de dossier du prêteur (= prix_achat ×
# ltv_achat). Aligné sur ``SCENARIO_ACHAT.ltv`` (75 %).
LTV_ACHAT_PRETEUR_B: float = 0.75


# ─── Configurations par scénario (R50, R57, R59 dans Excel) ──────

@dataclass(frozen=True)
class ScenarioConfig:
    name: str
    label: str
    ltv: float        # ratio prêt/valeur
    amort_annees: int
    rcd: float        # ratio couverture de dette


SCENARIO_ACHAT = ScenarioConfig(
    name="achat", label="Achat conventionnel",
    ltv=0.75, amort_annees=25, rcd=1.20,
)
SCENARIO_REFI_SCHL = ScenarioConfig(
    name="refi_schl", label="SCHL standard",
    ltv=0.85, amort_annees=35, rcd=1.30,
)
SCENARIO_REFI_APH_50 = ScenarioConfig(
    name="refi_aph_50",
    label="SCHL Efficacité énergétique (50 pts)",
    ltv=0.85, amort_annees=40, rcd=1.10,
)
SCENARIO_REFI_APH_100 = ScenarioConfig(
    name="refi_aph_100",
    label="SCHL Abordabilité + Efficacité (100 pts)",
    ltv=0.95, amort_annees=50, rcd=1.10,
)

REFI_SCENARIOS: List[ScenarioConfig] = [
    SCENARIO_REFI_SCHL, SCENARIO_REFI_APH_50, SCENARIO_REFI_APH_100,
]

# Slugs internes des 4 scénarios → instance ``ScenarioConfig`` de
# référence (valeurs hardcoded). Utilisé pour appliquer les overrides
# globaux du groupe BD ``scenarios_financement`` (juin 2026).
SCENARIO_BASES: Dict[str, ScenarioConfig] = {
    "achat": SCENARIO_ACHAT,
    "schl_std": SCENARIO_REFI_SCHL,
    "aph50": SCENARIO_REFI_APH_50,
    "aph100": SCENARIO_REFI_APH_100,
}


def resolve_scenario(
    slug: str,
    overrides: Optional[Dict[str, Dict[str, float]]] = None,
) -> ScenarioConfig:
    """Retourne le ``ScenarioConfig`` d'un scénario en appliquant les
    overrides globaux (groupe BD ``scenarios_financement``) par-dessus
    les constantes hardcoded.

    ``overrides`` : dict imbriqué ``{ slug: {"ltv":.., "amort":..,
    "rcd":..} }`` (clés partielles autorisées). Champ absent → on garde
    la valeur de la dataclass hardcoded (fallback ultime). ``name`` et
    ``label`` ne sont jamais externalisés.
    """
    base = SCENARIO_BASES[slug]
    ov = (overrides or {}).get(slug) or {}
    ltv = ov.get("ltv")
    amort = ov.get("amort")
    rcd = ov.get("rcd")
    return ScenarioConfig(
        name=base.name,
        label=base.label,
        ltv=float(ltv) if ltv is not None else base.ltv,
        amort_annees=int(amort) if amort is not None else base.amort_annees,
        rcd=float(rcd) if rcd is not None else base.rcd,
    )


# ─── Helpers ───────────────────────────────────────────────────────


def taxes_bienvenue_mtl(
    prix_achat: float,
    brackets: Optional[List[tuple]] = None,
) -> float:
    """Taxes de bienvenue de Montréal (tiers progressifs 2024-2025).
    Cf. L6 dans l'Excel.

    ``brackets`` (optionnel) : liste de tuples ``(seuil_haut,
    taux_fraction)`` triée par seuil croissant, le dernier palier ayant
    un ``seuil_haut`` ``math.inf`` (palier ouvert). Provient du défaut
    global ``taxes_bienvenue_mtl`` (groupe ``baremes_fiscaux``). Si
    ``None`` → fallback ``TAXES_BIENVENUE_MTL_BRACKETS`` (hardcoded)."""
    if prix_achat <= 0:
        return 0.0
    brackets = brackets if brackets else TAXES_BIENVENUE_MTL_BRACKETS
    taxe = 0.0
    seuil_bas = 0.0
    for seuil_haut, taux in brackets:
        if prix_achat <= seuil_haut:
            taxe += (prix_achat - seuil_bas) * taux
            return taxe
        taxe += (seuil_haut - seuil_bas) * taux
        seuil_bas = seuil_haut
    return taxe  # never reached


def pv_canadian(
    rate_annual: float, n_months: int, payment_monthly: float
) -> float:
    """Valeur actualisée d'un flux de paiements mensuels avec
    intérêt composé semestriellement (convention canadienne).

    Réplique exactement Excel `PV((1+rate/2)^(1/6)-1, n*12, pmt/12)`.
    Retourne une valeur **négative** quand le paiement est positif
    (cf. Excel), donc on l'inverse pour la consommer en `hypothèque
    maximale = -PV(...)`.
    """
    if n_months <= 0:
        return 0.0
    if rate_annual == 0:
        return -payment_monthly * n_months
    rate_monthly = (1 + rate_annual / 2) ** (1 / 6) - 1
    if rate_monthly == 0:
        return -payment_monthly * n_months
    factor = (1 - (1 + rate_monthly) ** (-n_months)) / rate_monthly
    return -payment_monthly * factor


def pmt_canadian(
    rate_annual: float, n_months: int, principal: float
) -> float:
    """Paiement mensuel pour un prêt avec intérêt composé
    semestriellement (convention canadienne). Inverse de `pv_canadian`.

    Réplique Excel `PMT((1+rate/2)^(1/6)-1, n*12, -principal)`.
    Retourne une valeur **positive** = paiement mensuel à débourser.
    """
    if n_months <= 0 or principal <= 0:
        return 0.0
    if rate_annual == 0:
        return principal / n_months
    rate_monthly = (1 + rate_annual / 2) ** (1 / 6) - 1
    if rate_monthly == 0:
        return principal / n_months
    return principal * rate_monthly / (1 - (1 + rate_monthly) ** (-n_months))


def solde_pret_canadien(
    principal: float,
    rate_annual: float,
    amort_annees: int,
    apres_annees: float,
) -> float:
    """Solde restant d'un prêt (composition canadienne semestrielle)
    après ``apres_annees`` années de paiements mensuels réguliers."""
    if principal <= 0:
        return 0.0
    n = amort_annees * 12
    k = min(int(round(apres_annees * 12)), n)
    if k <= 0:
        return principal
    pmt = pmt_canadian(rate_annual, n, principal)
    if rate_annual == 0:
        return max(0.0, principal - pmt * k)
    rm = (1 + rate_annual / 2) ** (1 / 6) - 1
    if rm == 0:
        return max(0.0, principal - pmt * k)
    solde = principal * (1 + rm) ** k - pmt * (((1 + rm) ** k - 1) / rm)
    return max(0.0, solde)


# ─── Agrégats de typologie ─────────────────────────────────────────


@dataclass
class TypologyAggregates:
    h13_loyer_pondere: float        # = Σ (G[t] × H[t]) / nb_total
    nb_abordables: int              # ceil(0.40 × nb_total)
    nb_pdm: int                     # nb_total - nb_abordables
    nouveau_loyer_moyen_pdm: float  # algo: unités les plus chères


def compute_typology_aggregates(
    typologie: Dict[str, int],
    typologie_prix: Dict[str, float],
    nb_total: int,
    ratio_abordabilite: float = RATIO_ABORDABILITE_APH,
) -> TypologyAggregates:
    """Calcule H13, nb_abordables, nouveau_loyer_moyen_pdm.

    `typologie` = { "2.5": 0, "3.5": 4, "4.5": 4, ... }
    `typologie_prix` = { "3.5": 1400, "4.5": 1600, ... } (uniquement
    pour les types avec quantité > 0).

    ``ratio_abordabilite`` : proportion des logements abordables (APH
    SELECT). Défaut ``RATIO_ABORDABILITE_APH`` (0.40). Externalisé via
    le défaut global ``ratio_abordabilite_aph``.
    """
    if nb_total <= 0:
        return TypologyAggregates(0.0, 0, 0, 0.0)

    # H13 = moyenne pondérée des loyers.
    # On ne divise que par les unités QUI ONT UN PRIX SAISI — sinon
    # les unités sans prix font baisser artificiellement la moyenne
    # (cas : utilisateur saisit le prix pour 8 × 4.5 mais oublie
    # 4 × 5.5 → la moyenne tombait à 16800/12 = 1400 au lieu de
    # 16800/8 = 2100). Si TOUTES les typos ont un prix, le résultat
    # est strictement identique à total/nb_total.
    total_loyers = 0.0
    nb_with_price = 0
    for typo, qty in typologie.items():
        if qty <= 0:
            continue
        prix = float(typologie_prix.get(typo, 0) or 0)
        if prix > 0:
            total_loyers += qty * prix
            nb_with_price += qty
    h13 = (
        total_loyers / nb_with_price if nb_with_price > 0 else 0.0
    )

    # APH SELECT : nombre abordables = ceil(40 % × total)
    nb_abordables = math.ceil(ratio_abordabilite * nb_total)
    nb_pdm = nb_total - nb_abordables

    # Loyer moyen PDM : on prend les unités les plus chères en
    # premier, jusqu'à atteindre nb_pdm. Moyenne pondérée du résultat.
    nouveau_loyer_pdm = 0.0
    if nb_pdm > 0:
        sorted_typos = sorted(
            [
                (typo, qty, float(typologie_prix.get(typo, 0) or 0))
                for typo, qty in typologie.items()
                if qty > 0
            ],
            key=lambda x: x[2],
            reverse=True,
        )
        restant = nb_pdm
        total_loyers_pdm = 0.0
        for _typo, qty, prix in sorted_typos:
            pris = min(restant, qty)
            total_loyers_pdm += pris * prix
            restant -= pris
            if restant == 0:
                break
        nouveau_loyer_pdm = (
            total_loyers_pdm / nb_pdm if nb_pdm > 0 else 0.0
        )

    return TypologyAggregates(
        h13_loyer_pondere=h13,
        nb_abordables=int(nb_abordables),
        nb_pdm=int(nb_pdm),
        nouveau_loyer_moyen_pdm=nouveau_loyer_pdm,
    )


# ─── Calcul des dépenses normalisées (R35..R46) ───────────────────


@dataclass
class DepensesBreakdown:
    inoccupation: float
    taxes_municipales: float
    taxes_scolaires: float
    assurances: float
    energie: float
    concierge: float
    entretien: float
    gestion: float
    wifi: float
    thermopompes: float
    autres: float
    #: Août 2026 — « Autres normalisations » (% des revenus bruts,
    #: défaut 1 %). Champ avec défaut pour rester compatible avec
    #: tout appel existant du dataclass.
    autres_normalisations: float = 0.0
    #: Sept. 2026 — intérêts ANNUELS de la balance de vente (mode
    #: institution traditionnelle : la BV court pendant la détention,
    #: c'est une dépense d'opération). 0 partout ailleurs.
    interets_balance_vente: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.inoccupation + self.taxes_municipales + self.taxes_scolaires
            + self.assurances + self.energie + self.concierge
            + self.entretien + self.gestion + self.wifi
            + self.thermopompes + self.autres
            + self.autres_normalisations
            + self.interets_balance_vente
        )


def compute_depenses_for_scenario(
    *,
    is_refi: bool,
    is_aph: bool,
    nb_log: int,
    revenus_totaux: float,
    taxes_municipales: float,
    taxes_scolaires: float,
    assurances: float,
    energie_base: float,
    reduction_energie_pct: float,
    depenses_autres: float,
    wifi_ajoute: bool,
    nb_thermopompes_ajoutees: int,
    taux_inoccupation_pct: float,
    bareme_overrides: Optional[Dict[str, float]] = None,
    seuil_bascule_log: int = SEUIL_BASCULE_BAREME_LOG,
    facteur_indexation: float = 1.0,
    interets_balance_vente_annuels: float = 0.0,
) -> DepensesBreakdown:
    """Calcule R35..R45 pour un scénario donné.

    ``facteur_indexation`` (sept. 2026, retour Phil) : multiplicateur
    appliqué aux dépenses RÉELLES (taxes, assurances, énergie, autres)
    — ex. (1,03)² pour un refi 2 ans plus tard à 3 %/an de croissance
    des dépenses. 1.0 = comportement historique. Le barème normalisé
    (concierge/entretien/gestion) reste calculé sur les revenus du
    scénario. ``interets_balance_vente_annuels`` : dépense annuelle de
    la balance de vente (mode institution traditionnelle).

    `is_refi` : True pour SCHL/APH (vs achat).
    `is_aph`  : True pour les programmes APH (Efficacité énergétique),
                False pour SCHL standard. Détermine si les
                thermopompes sont incluses dans les dépenses.

    Différences :
      - Énergie : multipliée par (1 - reduction_pct) en refi.
      - WIFI    : ajouté en refi si wifi_ajoute (SCHL + APH).
      - Thermopompes : **uniquement en APH** (efficacité énergétique).
        Dans l'Excel R44 col C (SCHL) = D5×J43 avec J43 vide → 0.
        R44 col D (APH 50) = D5×K43 avec K43 = 190 $/thermopompe.

    ``bareme_overrides`` (dict, optionnel) écrase poste par poste les
    valeurs hardcoded de ``BAREME``. Provient des défauts globaux
    ``ProspectionAnalysisDefault`` (groupe ``depenses_normalisees``).
    ``seuil_bascule_log`` : seuil de bascule petit/grand immeuble
    (défaut ``SEUIL_BASCULE_BAREME_LOG`` = 12).
    """
    bareme = dict(BAREME)
    if bareme_overrides:
        for k, v in bareme_overrides.items():
            if v is None:
                continue
            bareme[k] = float(v)

    inoccupation = taux_inoccupation_pct * revenus_totaux

    concierge_par_log = (
        bareme["concierge_lt12"]
        if nb_log < seuil_bascule_log
        else bareme["concierge_gte12"]
    )
    concierge = concierge_par_log * nb_log
    entretien = nb_log * bareme["entretien"]
    gestion_pct = (
        bareme["gestion_lt12"]
        if nb_log < seuil_bascule_log
        else bareme["gestion_gte12"]
    )
    gestion = gestion_pct * revenus_totaux
    # « Autres normalisations » : même assiette que la gestion (% des
    # revenus bruts). ``.get`` garde le moteur robuste si un override
    # partiel omettait la clé.
    autres_normalisations = (
        float(bareme.get("autres_normalisations_pct", 0.01))
        * revenus_totaux
    )

    if is_refi:
        energie = energie_base * (1.0 - reduction_energie_pct)
        wifi = (
            bareme["wifi_par_log"] * nb_log * 12 + bareme["internet_fixe"] * 12
            if wifi_ajoute
            else 0.0
        )
        # Thermopompes UNIQUEMENT en APH (pas SCHL standard).
        thermopompes = (
            nb_thermopompes_ajoutees * bareme["thermopompe"]
            if is_aph
            else 0.0
        )
    else:
        energie = energie_base
        wifi = 0.0
        thermopompes = 0.0

    f = float(facteur_indexation or 1.0)
    return DepensesBreakdown(
        inoccupation=inoccupation,
        taxes_municipales=taxes_municipales * f,
        taxes_scolaires=taxes_scolaires * f,
        assurances=assurances * f,
        energie=energie * f,
        concierge=concierge,
        entretien=entretien,
        gestion=gestion,
        wifi=wifi,
        thermopompes=thermopompes,
        autres=depenses_autres * f,
        autres_normalisations=autres_normalisations,
        interets_balance_vente=float(interets_balance_vente_annuels or 0.0),
    )


# ─── Calcul de la valeur économique + financement par scénario ────


@dataclass
class ScenarioResult:
    config: ScenarioConfig
    nb_log: int
    loyer_mois: float
    revenus_totaux: float
    depenses: DepensesBreakdown
    revenus_net: float
    valeur_eco_tga: float
    hyp_max_tga: float
    paiement_hyp_max: float
    hyp_max_rcd: float
    valeur_eco_rcd: float
    valeur_marchande: Optional[float]
    hyp_max_vm: Optional[float]
    valeur_retenue: float
    financement: float
    # Paiement mensuel actuel sur le `financement` au `taux_interet`
    # du scénario, amortissement = `config.amort_annees` × 12 mois.
    paiement_mensuel_actuel: float = 0.0
    # Cashflow annuel = revenus_net − (paiement_mensuel_actuel × 12).
    # Représente ce qu'il reste en poche chaque année après le service
    # de la dette, en supposant qu'on emprunte le plein `financement`.
    cashflow_annuel: float = 0.0
    mdf_necessaire: Optional[float] = None
    equite_a_la_fin: Optional[float] = None


def compute_scenario(
    *,
    config: ScenarioConfig,
    nb_log: int,
    loyer_mois: float,
    revenus_totaux: float,
    depenses: DepensesBreakdown,
    tga: float,
    taux_interet: float,
    valeur_marchande: Optional[float] = None,
) -> ScenarioResult:
    """Calcule valeur économique + financement pour un scénario."""
    revenus_net = revenus_totaux - depenses.total

    # Valeur éco TGA (R54)
    valeur_eco_tga = revenus_net / tga if tga > 0 else 0.0
    hyp_max_tga = valeur_eco_tga * config.ltv

    # Valeur éco RCD (R61 → R63 → R62)
    paiement_hyp_max = (
        revenus_net / config.rcd if config.rcd > 0 else 0.0
    )
    hyp_max_rcd = -pv_canadian(
        rate_annual=taux_interet,
        n_months=config.amort_annees * 12,
        payment_monthly=paiement_hyp_max / 12.0,
    )
    valeur_eco_rcd = (
        hyp_max_rcd / config.ltv if config.ltv > 0 else 0.0
    )

    # Valeur marchande (R65) — uniquement pour la colonne Achat
    hyp_max_vm: Optional[float] = None
    if valeur_marchande is not None:
        hyp_max_vm = valeur_marchande * config.ltv

    # Valeur retenue (R68) :
    #   - Achat : min(valeur_marchande, valeur_eco_rcd, valeur_eco_tga)
    #   - Refi  : min(valeur_eco_rcd, valeur_eco_tga)
    eco_min = min(valeur_eco_rcd, valeur_eco_tga)
    if valeur_marchande is not None:
        valeur_retenue = min(valeur_marchande, eco_min)
    else:
        valeur_retenue = eco_min

    # Financement total (R69)
    financement = valeur_retenue * config.ltv

    # Paiement mensuel actuel sur le `financement` (au taux du
    # scénario, amortissement = config.amort_annees) + cashflow.
    paiement_mensuel_actuel = pmt_canadian(
        rate_annual=taux_interet,
        n_months=config.amort_annees * 12,
        principal=financement,
    )
    cashflow_annuel = revenus_net - paiement_mensuel_actuel * 12.0

    return ScenarioResult(
        config=config,
        nb_log=nb_log,
        loyer_mois=loyer_mois,
        revenus_totaux=revenus_totaux,
        depenses=depenses,
        revenus_net=revenus_net,
        valeur_eco_tga=valeur_eco_tga,
        hyp_max_tga=hyp_max_tga,
        paiement_hyp_max=paiement_hyp_max,
        hyp_max_rcd=hyp_max_rcd,
        valeur_eco_rcd=valeur_eco_rcd,
        valeur_marchande=valeur_marchande,
        hyp_max_vm=hyp_max_vm,
        valeur_retenue=valeur_retenue,
        financement=financement,
        paiement_mensuel_actuel=paiement_mensuel_actuel,
        cashflow_annuel=cashflow_annuel,
    )


# ─── Frais de démarrage (L4..L19) ──────────────────────────────────


@dataclass
class FraisDemarrage:
    courtier_hypothecaire_1: float
    courtier_hypothecaire_2: float
    taxes_bienvenue: float
    evaluateur: float
    evaluateur_2: float
    inspection: float
    avocat: float
    notaire: float
    notaire_2: float
    rapport_efficacite: float
    frais_developpement: float
    frais_negociations: float
    frais_travaux: float
    # Frais de dossier du prêteur B (mai 2026). 2 % × prêt initial
    # (= prix_achat × ltv_achat) par défaut, modifiable via les
    # défauts globaux (clé ``frais_dossier_preteur_pct``).
    frais_dossier_preteur: float
    interets: float
    revenus_nets_pendant_projet: float
    #: Sept. 2026 — poste PERMANENT (retour Phil) : intérêts de la
    #: balance de vente pendant la phase projet (mode prêteur B).
    #: 0 sans balance de vente ; non finançable par défaut, gérable
    #: dans Paramètres (registre) comme les autres postes.
    interets_balance_vente: float = 0.0

    # Postes de frais de démarrage PERSONNALISÉS (dynamiques, juin
    # 2026). Liste d'items dont le montant $ est DÉJÀ calculé (cf.
    # ``compute_frais_custom_montant``) :
    #   {"id": str, "label_fr": str, "montant": float,
    #    "financable": bool}
    # Vide par défaut → aucun impact sur le calcul (rétrocompat). Ce
    # champ n'est PAS un montant scalaire : il est traité à part dans
    # ``total`` et dans la composition de la MDF (jamais itéré comme un
    # attribut $ nommé).
    frais_custom: List[dict] = field(default_factory=list)

    @property
    def frais_custom_total(self) -> float:
        """Somme des montants $ des postes personnalisés."""
        return sum(float(it.get("montant", 0) or 0) for it in self.frais_custom)

    @property
    def total(self) -> float:
        return (
            sum(
                [
                    self.courtier_hypothecaire_1,
                    self.courtier_hypothecaire_2,
                    self.taxes_bienvenue,
                    self.evaluateur,
                    self.evaluateur_2,
                    self.inspection,
                    self.avocat,
                    self.notaire,
                    self.notaire_2,
                    self.rapport_efficacite,
                    self.frais_developpement,
                    self.frais_negociations,
                    self.frais_travaux,
                    self.frais_dossier_preteur,
                    self.interets,
                    self.revenus_nets_pendant_projet,
                    self.interets_balance_vente,
                ]
            )
            + self.frais_custom_total
        )


def compute_frais_demarrage(
    *,
    prix_achat: float,
    duree_projet_annees: int,
    revenus_net_achat: float,
    financement_aph_100: float,
    mdf_preteur_b_pct: float,
    taux_interet_preteur_b_projet: float,
    frais_developpement: float = 0.0,
    frais_negociations: float = 0.0,
    frais_travaux: float = 0.0,
    frais_dossier_preteur_pct: float = DEFAULT_FRAIS_DOSSIER_PRETEUR_PCT,
    ltv_achat_preteur_b: float = LTV_ACHAT_PRETEUR_B,
    frais_fixes_overrides: Optional[Dict[str, float]] = None,
    pct_courtiers_overrides: Optional[Dict[str, float]] = None,
    taxes_bienvenue_brackets: Optional[List[tuple]] = None,
    frais_custom_defs: Optional[List[dict]] = None,
) -> FraisDemarrage:
    """Calcule L4..L19. `financement_aph_100` est utilisé pour le
    courtier hyp. 2 (1 % du financement APH 100 pts, le plus généreux).
    L17 = intérêts pendant projet
        ((1 - mdf_preteur_b_pct) × prix × taux_interet_preteur_b_projet × durée).
    L18 = revenus nets pendant projet (négatif si net négatif).

    ``frais_fixes_overrides`` (dict, optionnel) écrase les défauts
    hardcoded de ``FRAIS_FIXES`` poste par poste. Provient des défauts
    globaux ``ProspectionAnalysisDefault`` (groupe ``mdf_frais``).
    ``pct_courtiers_overrides`` même chose pour les % courtiers
    hypothécaires (clés ``courtier_hypothecaire_1`` / ``_2``).

    ``frais_dossier_preteur_pct`` : fraction (0.02 = 2 %) appliquée au
    prêt initial du prêteur B (= prix_achat × ltv_achat_preteur_b).
    Provient du défaut global ``frais_dossier_preteur_pct``.

    ``frais_custom_defs`` (liste, optionnel) : définitions des postes de
    frais de démarrage PERSONNALISÉS (groupe BD ``frais_mdf_custom``).
    Chaque item ``{id, label_fr, type_montant, valeur,
    financable_par_defaut}``. Leur montant $ est calculé ici selon
    ``type_montant`` (cf. ``compute_frais_custom_montant``) et stocké
    dans ``FraisDemarrage.frais_custom``. Liste vide / ``None`` →
    aucun poste personnalisé (résultat identique à avant)."""
    ff = dict(FRAIS_FIXES)
    if frais_fixes_overrides:
        for k, v in frais_fixes_overrides.items():
            if v is None:
                continue
            ff[k] = float(v)

    pc = dict(PCT_COURTIERS)
    if pct_courtiers_overrides:
        for k, v in pct_courtiers_overrides.items():
            if v is None:
                continue
            pc[k] = float(v)

    pret_initial_preteur_b = prix_achat * ltv_achat_preteur_b

    # Postes personnalisés : calcule le montant $ de chaque item selon
    # son ``type_montant`` et conserve son flag « finançable ». La base
    # du type ``pct_financement`` est le financement du best APH (même
    # base que le courtier hypothécaire 2).
    frais_custom: List[dict] = []
    for item in frais_custom_defs or []:
        if not isinstance(item, dict):
            continue
        montant = compute_frais_custom_montant(
            item, prix_achat, financement_aph_100
        )
        frais_custom.append(
            {
                "id": str(item.get("id", "")),
                "label_fr": str(item.get("label_fr", "")),
                "type_montant": item.get("type_montant", "fixe"),
                "valeur": item.get("valeur", 0),
                "montant": montant,
                "financable": bool(item.get("financable_par_defaut", False)),
            }
        )

    return FraisDemarrage(
        courtier_hypothecaire_1=pc["courtier_hypothecaire_1"] * prix_achat,
        courtier_hypothecaire_2=pc["courtier_hypothecaire_2"] * financement_aph_100,
        taxes_bienvenue=taxes_bienvenue_mtl(
            prix_achat, taxes_bienvenue_brackets
        ),
        evaluateur=ff["evaluateur"],
        evaluateur_2=ff["evaluateur_2"],
        inspection=ff["inspection"],
        avocat=ff["avocat"],
        notaire=ff["notaire"],
        notaire_2=ff["notaire_2"],
        rapport_efficacite=ff["rapport_efficacite"],
        frais_developpement=frais_developpement,
        frais_negociations=frais_negociations,
        frais_travaux=frais_travaux,
        # Frais de dossier du prêteur B : pct × prêt initial.
        # Inséré entre les travaux estimés et les intérêts pour
        # respecter l'ordre attendu dans la composition MDF.
        frais_dossier_preteur=frais_dossier_preteur_pct * pret_initial_preteur_b,
        # L17 : (1 - mdf_preteur_b_pct) × prix × taux_interet_preteur_b_projet × durée
        interets=(1 - mdf_preteur_b_pct) * prix_achat * taux_interet_preteur_b_projet * duree_projet_annees,
        # L18 : -revenus_net_achat × durée (négatif = pas de revenu
        # pendant le projet, donc coût)
        revenus_nets_pendant_projet=-revenus_net_achat * duree_projet_annees,
        frais_custom=frais_custom,
    )


# ─── Pipeline complet ──────────────────────────────────────────────


@dataclass
class FinanceInputs:
    """Inputs unifiés pour les 2 calculateurs."""

    # B3..B14 (auto-importés + défauts)
    adresse: str = ""
    prix_achat: float = 0.0
    nombre_logements: int = 0
    revenus_annuels: float = 0.0
    taxes_municipales: float = 0.0
    taxes_scolaires: float = 0.0
    assurances: float = 0.0
    energie: float = 0.0
    depenses_autres: float = 0.0
    tga: float = 0.04
    taux_interet_achat: float = 0.04

    # D4..D9 (manuel + défauts)
    nb_logements_ajoutes: int = 0
    nb_thermopompes_ajoutees: int = 0
    wifi_ajoute: bool = True
    reduction_energie_pct: float = 0.0
    taux_interet_refi: float = 0.0

    # Typologie (G6..G12 auto, H6..H12 manuel)
    typologie: Dict[str, int] = field(default_factory=dict)
    typologie_prix: Dict[str, float] = field(default_factory=dict)

    # L (frais)
    duree_projet_annees: int = 2
    frais_developpement: float = 0.0
    frais_negociations: float = 0.0
    frais_travaux: float = 0.0
    # Frais de dossier du prêteur B (fraction du prêt initial). Défaut
    # 0.02 (2 %), modifiable globalement via le défaut
    # ``frais_dossier_preteur_pct`` ou par override de fiche.
    frais_dossier_preteur_pct: float = DEFAULT_FRAIS_DOSSIER_PRETEUR_PCT

    # APH SELECT only (manuel)
    nouveau_loyer_abordable: float = 0.0

    # MDF prêteur B (en fraction, ex. 0.25 pour 25 %). Modifiable
    # selon le prêteur (peut monter à 0.35).
    mdf_preteur_b_pct: float = 0.25

    # Août 2026 — Stratégie d'acquisition de la fiche. 'preteur_b'
    # (défaut historique) ; les modes d'achat direct (conventionnel,
    # schl_std, aph_50, aph_100) arrivent en phase 2. Le moteur
    # l'écho dans les résultats pour que la fiche ET le PDF suivent.
    strategie: str = "preteur_b"

    # Août 2026 — Balance de vente (financement vendeur). Le montant
    # REMPLACE une partie du prêt du prêteur B (le cash à l'achat ne
    # change pas) ; son taux (fraction, 0.06 = 6 %) sert aux intérêts
    # de portage pendant le projet. 0 → calcul identique à avant.
    balance_vente_montant: float = 0.0
    balance_vente_taux_pct: float = 0.0

    # Sept. 2026 — Phase 2 du chantier stratégies : achat DIRECT
    # (conventionnel / SCHL / APH) avec détention et refinancement à
    # l'an N. Croissances annuelles composées (fractions, 0.03 = 3 %)
    # appliquées aux revenus et aux dépenses pendant la détention.
    projection_horizon_annees: int = 5
    croissance_loyers: float = 0.03
    croissance_depenses: float = 0.03

    # Sept. 2026 — Phase 3 : optimisation PAR UNITÉ. Liste de dicts
    # ``{typo, loyer_actuel, loyer_cible, optimiser}``. Vide →
    # comportement historique (toutes les unités au loyer cible
    # pondéré H13). Règle affinée (retour Phil 2026-09-02) : une unité
    # NON optimisée = loyer actuel × (1 + croissance_loyers)^durée
    # (croissance organique pendant le projet/la détention) ; une
    # unité optimisée = loyer cible.
    unites: List[dict] = field(default_factory=list)

    # Sept. 2026 — retour Phil : la stratégie « institution
    # traditionnelle » regroupe les 4 programmes sur la même page ;
    # ``programme_achat`` = celui RETENU pour financer l'achat (pilote
    # la MDF et le solde au refi). Les anciennes valeurs de stratégie
    # (conventionnel/schl_std/aph_50/aph_100) sont des alias de
    # « traditionnel » + programme_achat.
    programme_achat: str = "conventionnel"

    # True quand la fiche a explicitement choisi une stratégie
    # (chantier staging) : active l'indexation organique des loyers
    # non optimisés et des dépenses réelles au refi. False = calcul
    # historique INTACT (prod, golden tests).
    chantier_actif: bool = False

    # Référence de refinancement choisie MANUELLEMENT (retour Phil
    # 2026-09-02). Mode B : refi_schl | refi_aph_50 | refi_aph_100.
    # Mode traditionnel : conventionnel | schl_std | aph_50 | aph_100.
    # None/invalide → le meilleur automatiquement.
    refi_retenu: Optional[str] = None

    # Taux d'intérêt du prêteur B pendant la phase chantier
    # (8 % typique 2024-2025). Utilisé pour calculer L17 — intérêts
    # de portage pendant projet.
    taux_interet_preteur_b_projet: float = 0.08

    # Taux d'inoccupation hypothèse SCHL standard (3 %). Varie selon
    # le marché (Montréal centre vs régions). Appliqué aux revenus
    # totaux pour calculer la perte de loyer R35.
    taux_inoccupation_pct: float = 0.03

    # Overrides manuels des postes de frais de démarrage. Dict
    # `{ "evaluateur": 1800, "inspection": 2000, ... }` — chaque clé
    # présente remplace la valeur calculée par défaut. Keys autorisées
    # = attributs de `FraisDemarrage` dataclass.
    frais_demarrage_overrides: Dict[str, float] = field(default_factory=dict)

    # Liste des postes de frais de démarrage finançables par prêteur B.
    # Pour ces postes, on paie seulement `mdf_preteur_b_pct` en cash,
    # le reste s'ajoute au prêt. Défaut : rapport_efficacite,
    # frais_developpement, frais_travaux.
    frais_demarrage_financables: list[str] = field(default_factory=list)

    # Overrides GLOBAUX (par opposition à `frais_demarrage_overrides`
    # qui est par fiche) des frais one-shot. Provient de la table
    # ``prospection_analysis_defaults`` (groupe ``mdf_frais``) — chargé
    # à la création/exécution d'une analyse. Clés = sans le préfixe
    # ``frais_`` côté BD (ex. BD ``frais_evaluateur`` → ``evaluateur``).
    frais_fixes_overrides: Dict[str, float] = field(default_factory=dict)

    # Overrides globaux des pourcentages des courtiers hypothécaires
    # (L4 et L5). Clés : ``courtier_hypothecaire_1`` / ``_2``. Valeurs
    # en fraction (0.01 = 1 %).
    pct_courtiers_overrides: Dict[str, float] = field(default_factory=dict)

    # Juin 2026 — Frais de démarrage PERSONNALISÉS (dynamiques). Liste
    # de définitions ``{id, label_fr, type_montant, valeur,
    # financable_par_defaut}`` provenant du défaut global
    # ``frais_mdf_custom`` (groupe ``mdf_frais``, ``value_json``). Le
    # moteur calcule le montant $ de chaque poste et l'ajoute aux frais
    # de démarrage (logique « finançable » identique aux postes fixes).
    # Liste vide → aucun poste personnalisé (résultat identique à avant).
    frais_custom_defs: List[dict] = field(default_factory=list)

    # Juin 2026 — Dé-hardcodage du barème des dépenses normalisées SCHL.
    # Overrides GLOBAUX du barème ``BAREME`` (groupe BD
    # ``depenses_normalisees``). Clés = celles de ``BAREME``
    # (concierge_lt12, concierge_gte12, entretien, gestion_lt12,
    # gestion_gte12, wifi_par_log, internet_fixe, thermopompe). Valeurs
    # déjà converties dans l'unité interne attendue par le moteur (les %
    # de gestion en fraction, ex. 0.0425). Vide → fallback ``BAREME``.
    bareme_overrides: Dict[str, float] = field(default_factory=dict)

    # Seuil de bascule du barème petit/grand immeuble (concierge +
    # gestion). Défaut ``SEUIL_BASCULE_BAREME_LOG`` (12). Externalisé via
    # le défaut global ``seuil_bascule_bareme_log``.
    seuil_bascule_bareme_log: int = SEUIL_BASCULE_BAREME_LOG

    # Juin 2026 — Dé-hardcodage des scénarios de financement. Overrides
    # GLOBAUX des ratios LTV / amortissement / RCD des 4 scénarios
    # (groupe BD ``scenarios_financement``). Dict imbriqué
    # ``{ slug: {"ltv":.., "amort":.., "rcd":..} }`` avec slug ∈
    # {achat, schl_std, aph50, aph100}. Champ absent → fallback
    # constante hardcoded (``SCENARIO_*``).
    scenario_overrides: Dict[str, Dict[str, float]] = field(
        default_factory=dict
    )

    # Juin 2026 — Dé-hardcodage des barèmes fiscaux (groupe BD
    # ``baremes_fiscaux``).
    #
    # Ratio d'abordabilité APH SELECT (proportion de logements
    # abordables). Défaut ``RATIO_ABORDABILITE_APH`` (0.40). Externalisé
    # via le défaut global ``ratio_abordabilite_aph``.
    ratio_abordabilite_aph: float = RATIO_ABORDABILITE_APH

    # Barème progressif des taxes de bienvenue de Montréal. Liste de
    # tuples ``(seuil_haut, taux_fraction)`` (dernier palier =
    # ``math.inf``). ``None`` → fallback ``TAXES_BIENVENUE_MTL_BRACKETS``.
    # Externalisé via le défaut global ``taxes_bienvenue_mtl``
    # (``value_json``).
    taxes_bienvenue_brackets: Optional[List[tuple]] = None

    # Juin 2026 — Registre unifié des frais de démarrage : MASQUAGE de
    # postes. Liste des clés masquées : clé interne d'un poste FIXE
    # (ex. ``"evaluateur"``, ``"inspection"``) OU ``id`` d'un poste
    # PERSONNALISÉ. Provient du registre ``mdf_frais_registry`` (entrées
    # avec ``visible == false``). Un poste masqué est forcé à 0 $ APRÈS
    # le calcul des frais et l'application des overrides, AVANT le total
    # / le cash — il disparaît ainsi de la MDF et du prix d'acquisition.
    # Vide (défaut) → aucun masquage → résultat STRICTEMENT identique à
    # avant (le masquage est opt-in). Les postes NON masqués gardent
    # leurs formules et montants au centime près.
    frais_masques: list[str] = field(default_factory=list)


@dataclass
class FinanceResults:
    """Sortie consolidée du moteur (3 scénarios refi + achat)."""

    inputs: FinanceInputs
    typology: TypologyAggregates
    frais_demarrage: FraisDemarrage
    prix_acquisition: float
    # MDF si on finance avec un prêteur B (privé, hypothèque
    # conventionnelle 75 % LTV) : 25 % du prix d'achat + frais
    # de démarrage. Plus simple à comprendre que le MDF basé
    # sur la valeur économique retenue (cf. `achat.mdf_necessaire`).
    mdf_preteur_b: float

    achat: ScenarioResult
    refi_schl: ScenarioResult
    refi_aph_50: ScenarioResult
    # None si pas d'abordabilité applicable (calculateur officiel).
    refi_aph_100: Optional[ScenarioResult]

    best_refi_amount: float
    best_refi_program: str

    # Août 2026 — chantier stratégies d'acquisition : détail du prêt
    # du prêteur B (sur le prix, portion des frais financée, total) et
    # balance de vente retenue. Défauts 0 → rétrocompatible.
    pret_preteur_b_sur_prix: float = 0.0
    pret_preteur_b_frais_finances: float = 0.0
    pret_preteur_b_total: float = 0.0
    balance_vente_retenue: float = 0.0

    # Sept. 2026 — stratégie « institution traditionnelle » (les 4
    # programmes sur la même page, onglets Achat / Refinancement /
    # Projections). None pour la stratégie prêteur B.
    traditionnel: Optional[dict] = None
    # Projection long terme du mode prêteur B (après refi) — None hors
    # chantier.
    projection_preteur_b: Optional[list] = None

    def to_dict(self) -> dict:
        """Pour persistance JSON dans `LeadAnalysis.analysis_results_json`."""
        mdf_pct = (
            self.inputs.mdf_preteur_b_pct
            if self.inputs.mdf_preteur_b_pct is not None
            else 0.25
        )
        return {
            "frais_demarrage": _dataclass_to_dict(self.frais_demarrage),
            "frais_demarrage_total": self.frais_demarrage.total,
            "prix_acquisition": self.prix_acquisition,
            "mdf_preteur_b": self.mdf_preteur_b,
            # Composantes du MDF prêteur B (pour breakdown UI) :
            #   mdf_pct_prix_achat (X % × prix d'achat)
            # + frais_demarrage.total
            # = mdf_preteur_b
            "mdf_preteur_b_pct": mdf_pct,
            "taux_interet_preteur_b_projet": self.inputs.taux_interet_preteur_b_projet,
            "taux_inoccupation_pct": self.inputs.taux_inoccupation_pct,
            "frais_dossier_preteur_pct": self.inputs.frais_dossier_preteur_pct,
            # Alias conservé pour rétrocompat UI front-end.
            "mdf_25pct_prix_achat": mdf_pct * self.inputs.prix_achat,
            "mdf_pct_prix_achat": mdf_pct * self.inputs.prix_achat,
            "prix_achat": self.inputs.prix_achat,
            "frais_demarrage_financables": list(
                self.inputs.frais_demarrage_financables or []
            ),
            # Août 2026 — stratégie de la fiche + prêt du prêteur B.
            # La fiche ET le PDF s'appuient là-dessus (jamais sur un
            # flag frontend) pour adapter l'affichage.
            "strategie": self.inputs.strategie or "preteur_b",
            "pret_preteur_b": {
                "sur_prix": self.pret_preteur_b_sur_prix,
                "frais_finances": self.pret_preteur_b_frais_finances,
                "total": self.pret_preteur_b_total,
            },
            "balance_vente": {
                "montant": self.balance_vente_retenue,
                "taux_pct": float(
                    self.inputs.balance_vente_taux_pct or 0.0
                ) * 100.0,
            },
            "traditionnel": self.traditionnel,
            "projection_preteur_b": self.projection_preteur_b,
            # Phase 3 — résumé de l'optimisation par unité (None si la
            # fiche n'a pas détaillé ses unités).
            "unites": (
                {
                    "total": len(self.inputs.unites),
                    "optimisees": sum(
                        1
                        for u in self.inputs.unites
                        if isinstance(u, dict)
                        and u.get("optimiser", True)
                    ),
                }
                if self.inputs.unites
                else None
            ),
            "typology": {
                "h13_loyer_pondere": self.typology.h13_loyer_pondere,
                "nb_abordables": self.typology.nb_abordables,
                "nb_pdm": self.typology.nb_pdm,
                "nouveau_loyer_moyen_pdm": self.typology.nouveau_loyer_moyen_pdm,
            },
            "scenarios": {
                "achat": _scenario_to_dict(self.achat),
                "refi_schl": _scenario_to_dict(self.refi_schl),
                "refi_aph_50": _scenario_to_dict(self.refi_aph_50),
                "refi_aph_100": (
                    _scenario_to_dict(self.refi_aph_100)
                    if self.refi_aph_100 is not None
                    else None
                ),
            },
            "best_refi": {
                "amount": self.best_refi_amount,
                "program": self.best_refi_program,
            },
        }


def _dataclass_to_dict(d) -> dict:
    return {k: v for k, v in d.__dict__.items()}


def _scenario_to_dict(r: ScenarioResult) -> dict:
    return {
        "name": r.config.name,
        "label": r.config.label,
        "ltv": r.config.ltv,
        "amort_annees": r.config.amort_annees,
        "rcd": r.config.rcd,
        "nb_log": r.nb_log,
        "loyer_mois": r.loyer_mois,
        "revenus_totaux": r.revenus_totaux,
        "depenses_total": r.depenses.total,
        "depenses": _dataclass_to_dict(r.depenses),
        "revenus_net": r.revenus_net,
        "valeur_eco_tga": r.valeur_eco_tga,
        "valeur_eco_rcd": r.valeur_eco_rcd,
        "valeur_marchande": r.valeur_marchande,
        "valeur_retenue": r.valeur_retenue,
        "financement": r.financement,
        "paiement_mensuel_actuel": r.paiement_mensuel_actuel,
        "cashflow_annuel": r.cashflow_annuel,
        "mdf_necessaire": r.mdf_necessaire,
        "equite_a_la_fin": r.equite_a_la_fin,
    }


def compute_all(inputs: FinanceInputs, use_aph_select: bool = True) -> FinanceResults:
    """Pipeline complet d'analyse financière.

    `use_aph_select=True` (défaut) : calcule aussi le scénario
    APH 100 pts (avec abordabilité). Sinon seuls SCHL + APH 50 sont
    calculés (et `refi_aph_100` est dupliqué de `refi_aph_50` comme
    valeur de secours).

    Étapes :
      1. Agrégats de typologie (H13, abord/PDM)
      2. Scénario Achat (sans frais démarrage)
      3. Scénarios refi (sans frais démarrage)
      4. Frais de démarrage (L4..L19, basé sur achat + refi_aph_100)
      5. Prix d'acquisition = prix_achat + frais_demarrage.total
      6. MDF achat / équité refi
      7. Best refi
    """
    # Juin 2026 : configs des scénarios résolues depuis les overrides
    # globaux (groupe ``scenarios_financement``), avec fallback sur les
    # constantes hardcoded ``SCENARIO_*`` poste par poste.
    sc_ov = inputs.scenario_overrides
    cfg_achat = resolve_scenario("achat", sc_ov)
    cfg_schl = resolve_scenario("schl_std", sc_ov)
    cfg_aph50 = resolve_scenario("aph50", sc_ov)
    cfg_aph100 = resolve_scenario("aph100", sc_ov)

    typo = compute_typology_aggregates(
        inputs.typologie,
        inputs.typologie_prix,
        inputs.nombre_logements,
        ratio_abordabilite=inputs.ratio_abordabilite_aph,
    )

    # ── Étape 1 : Scénario Achat ─────────────────────────────────
    nb_log_achat = inputs.nombre_logements
    loyer_mois_achat = (
        inputs.revenus_annuels / 12.0 / nb_log_achat
        if nb_log_achat > 0
        else 0.0
    )
    depenses_achat = compute_depenses_for_scenario(
        is_refi=False,
        is_aph=False,
        nb_log=nb_log_achat,
        revenus_totaux=inputs.revenus_annuels,
        taxes_municipales=inputs.taxes_municipales,
        taxes_scolaires=inputs.taxes_scolaires,
        assurances=inputs.assurances,
        energie_base=inputs.energie,
        reduction_energie_pct=inputs.reduction_energie_pct,
        depenses_autres=inputs.depenses_autres,
        wifi_ajoute=inputs.wifi_ajoute,
        nb_thermopompes_ajoutees=inputs.nb_thermopompes_ajoutees,
        taux_inoccupation_pct=inputs.taux_inoccupation_pct,
        bareme_overrides=inputs.bareme_overrides,
        seuil_bascule_log=inputs.seuil_bascule_bareme_log,
    )
    achat = compute_scenario(
        config=cfg_achat,
        nb_log=nb_log_achat,
        loyer_mois=loyer_mois_achat,
        revenus_totaux=inputs.revenus_annuels,
        depenses=depenses_achat,
        tga=inputs.tga,
        taux_interet=inputs.taux_interet_achat,
        valeur_marchande=inputs.prix_achat,
    )

    # ── Étape 2 : Scénarios refi (sans abordabilité) ─────────────
    # Nb logements après refi
    nb_log_refi = inputs.nombre_logements + inputs.nb_logements_ajoutes

    # D8 OFFICIEL : loyer moyen = H13 (auto)
    nouveau_loyer_moyen = typo.h13_loyer_pondere

    # Croissances organiques (retour Phil 2026-09-02) : actives
    # seulement quand la fiche a choisi une stratégie (chantier) —
    # le calcul historique reste intact sinon.
    cl_organique = (
        float(inputs.croissance_loyers or 0.0)
        if inputs.chantier_actif
        else 0.0
    )
    cd_organique = (
        float(inputs.croissance_depenses or 0.0)
        if inputs.chantier_actif
        else 0.0
    )
    facteur_loyer_projet = (1 + cl_organique) ** max(
        0, inputs.duree_projet_annees
    )
    # Dépenses réelles au refi = dépenses ACTUELLES indexées pendant la
    # durée du projet (ex. ×1,03² pour 2 ans à 3 %).
    facteur_dep_projet = (1 + cd_organique) ** max(
        0, inputs.duree_projet_annees
    )

    # Phase 3 — optimisation PAR UNITÉ : loyers mensuels effectifs au
    # refi = cible pour les unités cochées ; une unité NON optimisée
    # suit la croissance ORGANIQUE pendant la durée du projet
    # (actuel × (1+cl)^durée). Les unités AJOUTÉES au refi sont
    # neuves → loyer cible pondéré H13.
    unites_valides = [
        u for u in (inputs.unites or []) if isinstance(u, dict)
    ]
    loyers_refi_unites: List[float] = []
    if unites_valides:
        for u in unites_valides:
            if u.get("optimiser", True):
                loyers_refi_unites.append(
                    float(u.get("loyer_cible") or 0)
                )
            else:
                loyers_refi_unites.append(
                    float(u.get("loyer_actuel") or 0)
                    * facteur_loyer_projet
                )
        loyers_refi_unites.extend(
            [nouveau_loyer_moyen] * max(0, inputs.nb_logements_ajoutes)
        )

    # Revenus refi standards (SCHL et APH 50) : tous au nouveau loyer —
    # ou, si les unités sont détaillées, la somme unité par unité.
    if loyers_refi_unites:
        revenus_refi_std = sum(loyers_refi_unites) * 12.0
        loyer_mois_refi_std = (
            revenus_refi_std / 12.0 / nb_log_refi
            if nb_log_refi > 0
            else 0.0
        )
    else:
        revenus_refi_std = nouveau_loyer_moyen * nb_log_refi * 12.0
        loyer_mois_refi_std = nouveau_loyer_moyen

    # Dépenses SCHL standard : pas de thermopompes (is_aph=False).
    depenses_schl = compute_depenses_for_scenario(
        is_refi=True,
        is_aph=False,
        nb_log=nb_log_refi,
        revenus_totaux=revenus_refi_std,
        taxes_municipales=inputs.taxes_municipales,
        taxes_scolaires=inputs.taxes_scolaires,
        assurances=inputs.assurances,
        energie_base=inputs.energie,
        reduction_energie_pct=inputs.reduction_energie_pct,
        depenses_autres=inputs.depenses_autres,
        wifi_ajoute=inputs.wifi_ajoute,
        nb_thermopompes_ajoutees=inputs.nb_thermopompes_ajoutees,
        taux_inoccupation_pct=inputs.taux_inoccupation_pct,
        bareme_overrides=inputs.bareme_overrides,
        seuil_bascule_log=inputs.seuil_bascule_bareme_log,
        facteur_indexation=facteur_dep_projet,
    )
    # Dépenses APH 50 : avec thermopompes (is_aph=True).
    depenses_aph_50 = compute_depenses_for_scenario(
        is_refi=True,
        is_aph=True,
        nb_log=nb_log_refi,
        revenus_totaux=revenus_refi_std,
        taxes_municipales=inputs.taxes_municipales,
        taxes_scolaires=inputs.taxes_scolaires,
        assurances=inputs.assurances,
        energie_base=inputs.energie,
        reduction_energie_pct=inputs.reduction_energie_pct,
        depenses_autres=inputs.depenses_autres,
        wifi_ajoute=inputs.wifi_ajoute,
        nb_thermopompes_ajoutees=inputs.nb_thermopompes_ajoutees,
        taux_inoccupation_pct=inputs.taux_inoccupation_pct,
        bareme_overrides=inputs.bareme_overrides,
        seuil_bascule_log=inputs.seuil_bascule_bareme_log,
        facteur_indexation=facteur_dep_projet,
    )
    refi_schl = compute_scenario(
        config=cfg_schl,
        nb_log=nb_log_refi,
        loyer_mois=loyer_mois_refi_std,
        revenus_totaux=revenus_refi_std,
        depenses=depenses_schl,
        tga=inputs.tga,
        taux_interet=inputs.taux_interet_refi,
        valeur_marchande=None,
    )
    refi_aph_50 = compute_scenario(
        config=cfg_aph50,
        nb_log=nb_log_refi,
        loyer_mois=loyer_mois_refi_std,
        revenus_totaux=revenus_refi_std,
        depenses=depenses_aph_50,
        tga=inputs.tga,
        taux_interet=inputs.taux_interet_refi,
        valeur_marchande=None,
    )

    # ── Étape 3 : Scénario APH 100 pts (avec abordabilité) ───────
    if use_aph_select and typo.nb_abordables > 0:
        if loyers_refi_unites:
            # Phase 3 : les nb_abordables unités les MOINS chères
            # passent au loyer abordable ; les autres gardent leur
            # loyer effectif (cible si optimisée, actuel sinon) —
            # même philosophie que la sélection PDM « les plus chères
            # d'abord » du calculateur officiel.
            tries = sorted(loyers_refi_unites)
            n_ab = min(typo.nb_abordables, len(tries))
            revenus_aph_100 = (
                n_ab * inputs.nouveau_loyer_abordable
                + sum(tries[n_ab:])
            ) * 12.0
        else:
            revenus_aph_100 = (
                typo.nb_abordables * inputs.nouveau_loyer_abordable
                + typo.nb_pdm * typo.nouveau_loyer_moyen_pdm
            ) * 12.0
        loyer_mois_aph_100 = (
            revenus_aph_100 / 12.0 / nb_log_refi if nb_log_refi > 0 else 0.0
        )
        depenses_aph_100 = compute_depenses_for_scenario(
            is_refi=True,
            is_aph=True,
            nb_log=nb_log_refi,
            revenus_totaux=revenus_aph_100,
            taxes_municipales=inputs.taxes_municipales,
            taxes_scolaires=inputs.taxes_scolaires,
            assurances=inputs.assurances,
            energie_base=inputs.energie,
            reduction_energie_pct=inputs.reduction_energie_pct,
            depenses_autres=inputs.depenses_autres,
            wifi_ajoute=inputs.wifi_ajoute,
            nb_thermopompes_ajoutees=inputs.nb_thermopompes_ajoutees,
            taux_inoccupation_pct=inputs.taux_inoccupation_pct,
            bareme_overrides=inputs.bareme_overrides,
            seuil_bascule_log=inputs.seuil_bascule_bareme_log,
            facteur_indexation=facteur_dep_projet,
        )
        refi_aph_100 = compute_scenario(
            config=cfg_aph100,
            nb_log=nb_log_refi,
            loyer_mois=loyer_mois_aph_100,
            revenus_totaux=revenus_aph_100,
            depenses=depenses_aph_100,
            tga=inputs.tga,
            taux_interet=inputs.taux_interet_refi,
            valeur_marchande=None,
        )
    else:
        # Pas d'abordabilité possible — on garde refi_aph_50 comme
        # « best APH » disponible et refi_aph_100 reste à None.
        refi_aph_100 = None  # type: ignore

    # ── Étape 4 : Frais de démarrage ─────────────────────────────
    # L5 (courtier hyp. 2) utilise le financement du « best APH »
    # disponible — APH 100 si abord, sinon APH 50 (= comportement
    # du calculateur OFFICIEL où col D = APH 50).
    financement_best_aph = (
        refi_aph_100.financement
        if refi_aph_100 is not None
        else refi_aph_50.financement
    )
    frais = compute_frais_demarrage(
        prix_achat=inputs.prix_achat,
        duree_projet_annees=inputs.duree_projet_annees,
        revenus_net_achat=achat.revenus_net,
        financement_aph_100=financement_best_aph,
        mdf_preteur_b_pct=inputs.mdf_preteur_b_pct,
        taux_interet_preteur_b_projet=inputs.taux_interet_preteur_b_projet,
        frais_developpement=inputs.frais_developpement,
        frais_negociations=inputs.frais_negociations,
        frais_travaux=inputs.frais_travaux,
        frais_dossier_preteur_pct=inputs.frais_dossier_preteur_pct,
        ltv_achat_preteur_b=cfg_achat.ltv,
        frais_fixes_overrides=inputs.frais_fixes_overrides,
        pct_courtiers_overrides=inputs.pct_courtiers_overrides,
        taxes_bienvenue_brackets=inputs.taxes_bienvenue_brackets,
        frais_custom_defs=inputs.frais_custom_defs,
    )
    # Overrides manuels : pour chaque clé fournie par l'utilisateur,
    # on remplace la valeur calculée par sa saisie. Permet d'ajuster
    # les frais sans casser les défauts pour les autres postes.
    if inputs.frais_demarrage_overrides:
        for k, v in inputs.frais_demarrage_overrides.items():
            if v is None:
                continue
            if hasattr(frais, k):
                setattr(frais, k, float(v))
        # Override PAR FICHE des postes PERSONNALISÉS (clé = id du poste,
        # comme les postes fixes). Permet d'ajuster un frais perso sur une
        # fiche sans toucher au défaut global.
        for _c in frais.frais_custom:
            _cid = str(_c.get("id", ""))
            if _cid and inputs.frais_demarrage_overrides.get(_cid) is not None:
                _c["montant"] = float(inputs.frais_demarrage_overrides[_cid])

    # ── Registre unifié des frais : MASQUAGE de postes ───────────
    # Juin 2026. APRÈS la construction de ``frais`` et l'application des
    # overrides (par fiche), AVANT le calcul du total / du cash : on
    # force à 0 $ chaque poste masqué (visible:false dans le registre
    # ``mdf_frais_registry``). Postes FIXES masqués par clé interne
    # (attribut de ``FraisDemarrage``) ; postes PERSONNALISÉS masqués par
    # ``id`` (retirés de ``frais.frais_custom``). On NE TOUCHE À RIEN
    # d'autre : les formules et montants des postes NON masqués restent
    # identiques au centime. Liste vide → aucun masquage (no-op).
    masques = set(inputs.frais_masques or [])
    if masques:
        for _mk in masques:
            # ``frais_custom`` est une LISTE (postes perso) — jamais un
            # montant scalaire : on ne la met PAS à 0.0 ici (les perso
            # sont retirés ci-dessous par leur ``id``).
            if _mk == "frais_custom":
                continue
            if hasattr(frais, _mk):
                setattr(frais, _mk, 0.0)
        # Postes personnalisés masqués : on les retire de la liste pour
        # qu'ils ne contribuent ni au total, ni au cash, ni à l'affichage.
        frais.frais_custom = [
            _c for _c in frais.frais_custom
            if str(_c.get("id", "")) not in masques
        ]

    # ── Intérêts de portage sur le PRÊT DE CONSTRUCTION COMPLET ──
    # Le prêteur B finance aussi la portion finançable des frais de
    # démarrage (Σ frais finançables × (1−MDF%)), EN PLUS de (1−MDF%)×prix.
    # Ces montants prêtés génèrent eux aussi des intérêts pendant le
    # projet. On recalcule donc ``interets`` sur (1−MDF%) × (prix + Σ frais
    # finançables) × taux × durée — cohérent avec le « prêt de
    # construction » du TRI. Cocher/décocher un frais finançable fait
    # désormais bouger les intérêts. (``interets`` n'est jamais lui-même
    # finançable → pas de circularité.) Doit rester AVANT ``frais.total``.
    _mdf_pct = (
        inputs.mdf_preteur_b_pct
        if inputs.mdf_preteur_b_pct is not None
        else 0.25
    )
    _fin = set(inputs.frais_demarrage_financables or [])
    _frais_fin_total = 0.0
    for _k, _v in frais.__dict__.items():
        if _k in ("frais_custom", "interets"):
            continue
        if _k in _fin:
            try:
                _frais_fin_total += float(_v or 0)
            except (TypeError, ValueError):
                pass
    for _c in frais.frais_custom:
        if str(_c.get("id", "")) in _fin:
            try:
                _frais_fin_total += float(_c.get("montant", 0) or 0)
            except (TypeError, ValueError):
                pass
    # Balance de vente (règle affinée 2026-09-02) : elle REMPLACE une
    # partie du CASH de la mise de fonds — le prêt B, lui, ne bouge
    # pas. Ses intérêts pendant le projet vivent dans un poste
    # PERMANENT distinct (« Intérêts balance de vente », 0 sans BV),
    # non finançable par défaut et gérable dans Paramètres comme les
    # autres postes.
    _bv = max(0.0, float(inputs.balance_vente_montant or 0.0))
    _bv = min(_bv, _mdf_pct * inputs.prix_achat)
    frais.interets = (
        (1 - _mdf_pct)
        * (inputs.prix_achat + _frais_fin_total)
        * inputs.taux_interet_preteur_b_projet
        * inputs.duree_projet_annees
    )
    frais.interets_balance_vente = (
        _bv
        * float(inputs.balance_vente_taux_pct or 0.0)
        * inputs.duree_projet_annees
    )

    prix_acquisition = inputs.prix_achat + frais.total
    # MDF avec prêteur B = X % prix achat + frais démarrage cash. X
    # est `mdf_preteur_b_pct` (défaut 25 %, parfois 35 %).
    # Certains postes de frais sont FINANÇABLES par le prêteur B :
    # pour ceux-là, on paie seulement X % en cash (le reste est
    # ajouté au prêt). Les autres postes sont payés 100 % cash.
    mdf_pct = (
        inputs.mdf_preteur_b_pct
        if inputs.mdf_preteur_b_pct is not None
        else 0.25
    )
    financables = set(inputs.frais_demarrage_financables or [])
    frais_cash_total = 0.0
    # Portion des frais FINANCÉE par le prêteur B (complément du cash
    # des postes finançables) — sert à afficher le prêt B total.
    frais_finance_total = 0.0
    for k, v in frais.__dict__.items():
        # ``frais_custom`` est une LISTE (postes personnalisés), pas un
        # montant scalaire : traité séparément ci-dessous pour éviter
        # un ``float(list)``.
        if k == "frais_custom":
            continue
        amount = float(v or 0)
        if k in financables:
            frais_cash_total += amount * mdf_pct
            frais_finance_total += amount * (1 - mdf_pct)
        else:
            frais_cash_total += amount
    # Postes personnalisés : même logique « finançable » que les postes
    # fixes — si finançable, seul ``mdf_pct`` est payé cash (le reste
    # est financé) ; sinon 100 % cash. Liste vide → contribution nulle.
    for item in frais.frais_custom:
        amount = float(item.get("montant", 0) or 0)
        # Finançabilité PAR FICHE (set `financables`, clé = id du poste),
        # comme les postes fixes — surchargeable dans la fiche. On reflète
        # l'état EFFECTIF dans le JSON (`financable`) pour le PDF/affichage.
        _cid = str(item.get("id", ""))
        is_fin = _cid in financables
        item["financable"] = is_fin
        if is_fin:
            frais_cash_total += amount * mdf_pct
            frais_finance_total += amount * (1 - mdf_pct)
        else:
            frais_cash_total += amount

    # Balance de vente (2026-09-02) : le vendeur finance une partie de
    # la MISE DE FONDS — le X % × prix à sortir en cash est réduit du
    # montant de la BV (jamais sous 0), le prêt B reste entier.
    balance_vente = min(
        max(0.0, float(inputs.balance_vente_montant or 0.0)),
        mdf_pct * inputs.prix_achat,
    )
    mdf_preteur_b = (
        mdf_pct * inputs.prix_achat - balance_vente + frais_cash_total
    )

    # ── Prêt accordé par le prêteur B (retour Phil 2026-08-31 : le
    # montant du prêt doit apparaître, en complément de la MDF).
    pret_b_sur_prix = (1 - mdf_pct) * inputs.prix_achat
    pret_preteur_b_total = pret_b_sur_prix + frais_finance_total

    # ── Étape 5 : MDF achat / équité refi ────────────────────────
    achat.mdf_necessaire = prix_acquisition - achat.financement
    refi_schl.equite_a_la_fin = refi_schl.financement - prix_acquisition
    refi_aph_50.equite_a_la_fin = refi_aph_50.financement - prix_acquisition
    if refi_aph_100 is not None:
        refi_aph_100.equite_a_la_fin = (
            refi_aph_100.financement - prix_acquisition
        )

    # ── Étape 6 : Best refi ──────────────────────────────────────
    # Le MEILLEUR est calculé automatiquement ; l'utilisateur peut
    # choisir une RÉFÉRENCE différente (refi_retenu) qui pilote alors
    # le verdict, la projection et la carte kanban (retour Phil
    # 2026-09-02).
    candidates = [refi_schl, refi_aph_50]
    if refi_aph_100 is not None:
        candidates.append(refi_aph_100)
    meilleur_b = max(candidates, key=lambda s: s.equite_a_la_fin or 0.0)
    _refs_b = {"refi_schl": refi_schl, "refi_aph_50": refi_aph_50}
    if refi_aph_100 is not None:
        _refs_b["refi_aph_100"] = refi_aph_100
    best = _refs_b.get(inputs.refi_retenu or "") or meilleur_b
    best_amount = best.equite_a_la_fin or 0.0
    best_program = best.config.label

    # ── Phase 2 (sept. 2026) — Achat DIRECT + détention + refi an N ──
    # Stratégies « j'achète au programme (conventionnel / SCHL / APH)
    # sur les loyers ACTUELS, je détiens ~5 ans avec croissance des
    # revenus et des dépenses, et je regarde si le refinancement me
    # ressort tout mon cash ». Réponses Phil 2026-08-31.
    # ── Stratégie « institution traditionnelle » (2026-09-02) ────
    # Retour Phil : les 4 programmes (conventionnel / SCHL / APH 50 /
    # APH 100) sur la MÊME page, en deux onglets — Achat (loyers et
    # dépenses ACTUELS) et Refinancement à l\'an H (loyers optimisés +
    # croissance organique, dépenses réelles indexées). Le programme
    # RETENU pilote la mise de fonds et le solde du prêt au refi. Les
    # anciennes stratégies « achat direct » sont des alias.
    traditionnel: Optional[dict] = None
    _alias_trad = {"conventionnel", "schl_std", "aph_50", "aph_100"}
    _est_trad = inputs.strategie == "traditionnel" or (
        inputs.strategie in _alias_trad
    )
    if _est_trad:
        # Une ancienne stratégie « achat direct » (alias) PORTE le
        # programme ; « traditionnel » lit programme_achat.
        if inputs.strategie in _alias_trad:
            programme = inputs.strategie
        elif inputs.programme_achat in _alias_trad:
            programme = inputs.programme_achat
        else:
            programme = "conventionnel"
        _cfg_par_prog = {
            "conventionnel": resolve_scenario("achat", sc_ov),
            "schl_std": cfg_schl,
            "aph_50": cfg_aph50,
            "aph_100": cfg_aph100,
        }
        _labels_prog = {
            "conventionnel": "Conventionnel",
            "schl_std": "SCHL standard",
            "aph_50": "SCHL Efficacité (50 pts)",
            "aph_100": "SCHL Abordabilité + Efficacité (100 pts)",
        }
        h_annees = max(1, int(inputs.projection_horizon_annees or 5))
        cl = float(inputs.croissance_loyers or 0.0)
        cd = float(inputs.croissance_depenses or 0.0)
        bv_trad = max(0.0, float(inputs.balance_vente_montant or 0.0))
        bv_interets_annuels = bv_trad * float(
            inputs.balance_vente_taux_pct or 0.0
        )

        # ── Onglet ACHAT : 4 colonnes sur les loyers/dépenses ACTUELS
        # (+ intérêts annuels de la balance de vente en dépense — la
        # ligne est PERMANENTE : 0 sans BV).
        dep_achat_trad = compute_depenses_for_scenario(
            is_refi=False,
            is_aph=False,
            nb_log=nb_log_achat,
            revenus_totaux=inputs.revenus_annuels,
            taxes_municipales=inputs.taxes_municipales,
            taxes_scolaires=inputs.taxes_scolaires,
            assurances=inputs.assurances,
            energie_base=inputs.energie,
            reduction_energie_pct=inputs.reduction_energie_pct,
            depenses_autres=inputs.depenses_autres,
            wifi_ajoute=inputs.wifi_ajoute,
            nb_thermopompes_ajoutees=inputs.nb_thermopompes_ajoutees,
            taux_inoccupation_pct=inputs.taux_inoccupation_pct,
            bareme_overrides=inputs.bareme_overrides,
            seuil_bascule_log=inputs.seuil_bascule_bareme_log,
            interets_balance_vente_annuels=bv_interets_annuels,
        )
        achat_cols: dict = {}
        for prog, cfg_p in _cfg_par_prog.items():
            achat_cols[prog] = compute_scenario(
                config=cfg_p,
                nb_log=nb_log_achat,
                loyer_mois=loyer_mois_achat,
                revenus_totaux=inputs.revenus_annuels,
                depenses=dep_achat_trad,
                tga=inputs.tga,
                taux_interet=inputs.taux_interet_achat,
                valeur_marchande=inputs.prix_achat,
            )

        # ── Frais d\'acquisition (pas de phase chantier) : pas de 2e
        # courtier/évaluateur/notaire, ni portage, ni revenus pendant
        # projet ; rapport d\'efficacité gardé si le programme RETENU
        # est un APH ; intérêts BV = dépense annuelle, pas un frais.
        f_trad = {
            k: float(v or 0)
            for k, v in frais.__dict__.items()
            if k != "frais_custom"
        }
        for _k in (
            "courtier_hypothecaire_2", "evaluateur_2", "notaire_2",
            "interets", "revenus_nets_pendant_projet",
            "interets_balance_vente",
        ):
            f_trad[_k] = 0.0
        if programme not in ("aph_50", "aph_100"):
            f_trad["rapport_efficacite"] = 0.0
        _custom_total = sum(
            float(c.get("montant", 0) or 0) for c in frais.frais_custom
        )
        frais_trad_total = round(sum(f_trad.values()) + _custom_total, 2)

        # MDF par colonne = prix + frais − prêt − balance de vente (la
        # BV finance une partie de la mise de fonds).
        def _mdf_prog(prog: str) -> float:
            return max(
                0.0,
                inputs.prix_achat
                - achat_cols[prog].financement
                - bv_trad,
            ) + frais_trad_total

        pret_retenu = achat_cols[programme].financement
        mdf_cash = _mdf_prog(programme)

        # ── Revenus/dépenses à l\'an H (refinancement) ──
        def _rev_trad(a: int) -> float:
            if a <= 0:
                return inputs.revenus_annuels
            if not unites_valides:
                return inputs.revenus_annuels * (1 + cl) ** a
            total_mois = 0.0
            for u in unites_valides:
                if u.get("optimiser", False):
                    total_mois += (
                        float(u.get("loyer_cible") or 0)
                        * (1 + cl) ** max(0, a - 1)
                    )
                else:
                    total_mois += (
                        float(u.get("loyer_actuel") or 0) * (1 + cl) ** a
                    )
            return total_mois * 12.0

        rev_h = _rev_trad(h_annees)
        facteur_dep_h = (1 + cd) ** h_annees
        solde_retenu_h = solde_pret_canadien(
            pret_retenu,
            inputs.taux_interet_achat,
            _cfg_par_prog[programme].amort_annees,
            h_annees,
        )

        refi_cols: dict = {}
        for prog, cfg_p in _cfg_par_prog.items():
            dep_refi_p = compute_depenses_for_scenario(
                is_refi=True,
                is_aph=prog in ("aph_50", "aph_100"),
                nb_log=nb_log_achat,
                revenus_totaux=rev_h,
                taxes_municipales=inputs.taxes_municipales,
                taxes_scolaires=inputs.taxes_scolaires,
                assurances=inputs.assurances,
                energie_base=inputs.energie,
                reduction_energie_pct=inputs.reduction_energie_pct,
                depenses_autres=inputs.depenses_autres,
                wifi_ajoute=inputs.wifi_ajoute,
                nb_thermopompes_ajoutees=inputs.nb_thermopompes_ajoutees,
                taux_inoccupation_pct=inputs.taux_inoccupation_pct,
                bareme_overrides=inputs.bareme_overrides,
                seuil_bascule_log=inputs.seuil_bascule_bareme_log,
                facteur_indexation=facteur_dep_h,
            )
            sc_r = compute_scenario(
                config=cfg_p,
                nb_log=nb_log_achat,
                loyer_mois=(
                    rev_h / 12.0 / nb_log_achat if nb_log_achat else 0.0
                ),
                revenus_totaux=rev_h,
                depenses=dep_refi_p,
                tga=inputs.tga,
                taux_interet=inputs.taux_interet_refi,
                valeur_marchande=None,
            )
            # Le refi rembourse le prêt d\'achat retenu ET la balance
            # de vente : ce qui reste = le cash dégagé.
            sc_r.equite_a_la_fin = (
                sc_r.financement - solde_retenu_h - bv_trad
            )
            refi_cols[prog] = sc_r

        meilleur_prog = max(
            refi_cols, key=lambda k: refi_cols[k].equite_a_la_fin or 0.0
        )
        best_prog = (
            inputs.refi_retenu
            if inputs.refi_retenu in refi_cols
            else meilleur_prog
        )
        best_dispo = refi_cols[best_prog].equite_a_la_fin or 0.0

        # ── Projection long terme : détention, refi à l\'an H (au
        # programme gagnant), puis poursuite de la détention.
        dep0_trad = dep_achat_trad.total - bv_interets_annuels
        pret_best_refi = refi_cols[best_prog].financement
        projection = []
        for a in range(0, max(h_annees, 12) + 1):
            rev_a = _rev_trad(a)
            dep_a = dep0_trad * (1 + cd) ** a + (
                bv_interets_annuels if a < h_annees else 0.0
            )
            rno_a = rev_a - dep_a
            valeur_a = rno_a / inputs.tga if inputs.tga > 0 else 0.0
            if a <= h_annees:
                solde_a = (
                    solde_pret_canadien(
                        pret_retenu,
                        inputs.taux_interet_achat,
                        _cfg_par_prog[programme].amort_annees,
                        a,
                    )
                    + bv_trad
                )
            else:
                solde_a = solde_pret_canadien(
                    pret_best_refi,
                    inputs.taux_interet_refi,
                    _cfg_par_prog[best_prog].amort_annees,
                    a - h_annees,
                )
            projection.append({
                "annee": a,
                "revenus": round(rev_a, 2),
                "depenses": round(dep_a, 2),
                "rno": round(rno_a, 2),
                "valeur": round(valeur_a, 2),
                "solde_pret": round(solde_a, 2),
                "equite": round(valeur_a - solde_a, 2),
            })

        traditionnel = {
            "programme_retenu": programme,
            "labels": _labels_prog,
            "horizon": h_annees,
            "croissance_loyers": cl,
            "croissance_depenses": cd,
            "balance_vente": round(bv_trad, 2),
            "interets_bv_annuels": round(bv_interets_annuels, 2),
            "frais_demarrage": {
                k: round(v, 2) for k, v in f_trad.items()
            },
            "frais_demarrage_total": frais_trad_total,
            "mdf_cash": round(mdf_cash, 2),
            "pret_retenu": round(pret_retenu, 2),
            "solde_retenu_an_h": round(solde_retenu_h, 2),
            "achat": {
                p: _scenario_to_dict(s) for p, s in achat_cols.items()
            },
            "mdf_par_programme": {
                p: round(_mdf_prog(p), 2) for p in achat_cols
            },
            "refi": {
                p: _scenario_to_dict(s) for p, s in refi_cols.items()
            },
            "projection": projection,
            #: Le meilleur AUTOMATIQUE (étoile) — la référence
            #: (best_refi) peut être un autre programme, choisi
            #: manuellement (refi_retenu).
            "meilleur_refi_key": meilleur_prog,
            "best_refi": {
                "key": best_prog,
                "label": _labels_prog[best_prog],
                "argent_dispo": round(best_dispo, 2),
                "refi_possible": best_dispo >= mdf_cash - 0.005,
                "manque": round(max(0.0, mdf_cash - best_dispo), 2),
            },
        }

    # ── Projection long terme du mode PRÊTEUR B (2026-09-02) : après
    # le refi (an = durée du projet), le scénario gagnant continue de
    # croître — même graphique que le mode traditionnel.
    projection_preteur_b: Optional[list] = None
    if inputs.chantier_actif and not _est_trad:
        # Même RÉFÉRENCE que le verdict (choisie ou meilleure).
        best_b = best
        cl_b = float(inputs.croissance_loyers or 0.0)
        cd_b = float(inputs.croissance_depenses or 0.0)
        d0 = inputs.duree_projet_annees
        projection_preteur_b = []
        for a in range(0, 13):
            rev_a = best_b.revenus_totaux * (1 + cl_b) ** a
            dep_a = best_b.depenses.total * (1 + cd_b) ** a
            rno_a = rev_a - dep_a
            valeur_a = rno_a / inputs.tga if inputs.tga > 0 else 0.0
            solde_a = solde_pret_canadien(
                best_b.financement,
                inputs.taux_interet_refi,
                best_b.config.amort_annees,
                a,
            )
            projection_preteur_b.append({
                "annee": d0 + a,
                "revenus": round(rev_a, 2),
                "depenses": round(dep_a, 2),
                "rno": round(rno_a, 2),
                "valeur": round(valeur_a, 2),
                "solde_pret": round(solde_a, 2),
                "equite": round(valeur_a - solde_a, 2),
            })

    return FinanceResults(
        inputs=inputs,
        typology=typo,
        frais_demarrage=frais,
        prix_acquisition=prix_acquisition,
        mdf_preteur_b=mdf_preteur_b,
        achat=achat,
        refi_schl=refi_schl,
        refi_aph_50=refi_aph_50,
        refi_aph_100=refi_aph_100,
        best_refi_amount=best_amount,
        best_refi_program=best_program,
        pret_preteur_b_sur_prix=pret_b_sur_prix,
        pret_preteur_b_frais_finances=frais_finance_total,
        pret_preteur_b_total=pret_preteur_b_total,
        balance_vente_retenue=balance_vente,
        traditionnel=traditionnel,
        projection_preteur_b=projection_preteur_b,
    )
