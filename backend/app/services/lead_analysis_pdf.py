"""Génère le PDF complet d'une `LeadAnalysis` — export d'aide à la
décision pour les associés et collaborateurs prospection.

Refonte visuelle 2026 : layout A4 portrait, palette doré/ambre (accent)
+ vert (positif / scénario gagnant) + gris neutres sur fond blanc.
Pagination automatique via `SimpleDocTemplate` + `story=[...]`, en-tête
de page (adresse + date) et pied de page sur chaque page (`onFirstPage`
+ `onLaterPages`).

Sections (dans cet ordre) :
    1. En-tête (logo + titre + identité de l'immeuble)
    2. Bande de résultats clés (tuiles)
    3. Informations financières — état actuel
    4. Typologie des logements
    5. Inputs manuels d'analyse
    6. Composition MDF prêteur B
    7. Scénarios de financement (tableau comparatif + gagnant mis en valeur)
    8. Meilleur scénario refi (avec RCI/PVI calculés)
    9. Rendement de l'investisseur (TRI)            ← NOUVEAU
   10. Validation (warnings post-extraction)
   11. Sources & attachments

ReportLab pur Python — pas de dépendance système, compatible Render
Free. Aucune persistance : on régénère à la volée à chaque export.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead_analysis import (
    LeadAnalysis,
    LeadAnalysisAttachment,
)


log = logging.getLogger(__name__)


_LOGO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets",
    "logo.png",
)

# Mois français-CA pour formater la date de génération au format
# « 28 mai 2026 » plutôt que « 2026-05-28 ».
_MONTHS_FR_CA = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)

# Ordre canonique des typologies dans le tableau.
_TYPO_ORDER = ("1.5", "2.5", "3.5", "4.5", "5.5", "6.5", "7.5", "8.5")

# ── Palette 2026 ────────────────────────────────────────────────────
# Doré/ambre = accent (titres de section, en-tête) ; vert = positif /
# scénario gagnant ; gris neutres ; fond blanc.
_C_DARK = "#1a1a1a"          # texte principal
_C_MUTED = "#6b7280"         # texte secondaire
_C_LINE = "#e5e7eb"          # filets de tableaux
_C_AMBER = "#b45309"         # accent doré/ambre foncé (titres)
_C_AMBER_SOFT = "#fef3c7"    # fond ambre pâle (en-têtes de tableaux)
_C_AMBER_LINE = "#f59e0b"    # bordure ambre vive
_C_GREEN = "#047857"         # vert positif
_C_GREEN_SOFT = "#d1fae5"    # fond vert pâle (gagnant / OK)
_C_GREEN_LINE = "#10b981"    # bordure verte
_C_GREY_SOFT = "#f3f4f6"     # fond gris pâle (tuiles neutres)
_C_RED = "#b91c1c"           # négatif (cashflow < 0)

# Défauts des intrants manuels du TRI quand la fiche n'a rien de
# persisté — dupliqués de `lead_analyses._TRI_DEFAULTS` pour garder ce
# service autonome (cf. RÈGLE « réutilise/duplique la dérivation »).
_TRI_DEFAULTS = {
    "capital": None,
    "pct": 0.5,
    "cr_loyers": 0.03,
    "cr_dep": 0.03,
}


def _lazy_reportlab() -> Dict[str, Any]:
    from reportlab.lib import colors  # type: ignore
    from reportlab.lib.pagesizes import A4  # type: ignore
    from reportlab.lib.styles import (  # type: ignore
        ParagraphStyle,
        getSampleStyleSheet,
    )
    from reportlab.lib.units import mm  # type: ignore
    from reportlab.platypus import (  # type: ignore
        Image,
        KeepTogether,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    return {
        "colors": colors,
        "A4": A4,
        "ParagraphStyle": ParagraphStyle,
        "getSampleStyleSheet": getSampleStyleSheet,
        "mm": mm,
        "Image": Image,
        "KeepTogether": KeepTogether,
        "PageBreak": PageBreak,
        "Paragraph": Paragraph,
        "SimpleDocTemplate": SimpleDocTemplate,
        "Spacer": Spacer,
        "Table": Table,
        "TableStyle": TableStyle,
    }


def _money(n: Optional[float | int]) -> str:
    """Format français-CA, ex. « 1 234 567 $ » (entier). Identique au
    helper `formatMoney` côté frontend."""
    if n is None:
        return "—"
    try:
        rounded = round(float(n))
    except (TypeError, ValueError):
        return "—"
    sign = "-" if rounded < 0 else ""
    abs_str = f"{abs(rounded):,}".replace(",", " ")
    return f"{sign}{abs_str} $"


def _money_decimal(n: Optional[float | int]) -> str:
    """Format avec décimales pour montants précis (mensualités)."""
    if n is None:
        return "—"
    try:
        return f"{float(n):,.2f} $".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _pct(n: Optional[float | int], decimals: int = 2) -> str:
    if n is None:
        return "—"
    try:
        return f"{float(n):.{decimals}f} %"
    except (TypeError, ValueError):
        return "—"


def _pct_fraction(n: Optional[float | int], decimals: int = 1) -> str:
    """Formate une FRACTION (0.157 → « 15.7 % »). Pour les TRI et les
    croissances stockées en fraction."""
    if n is None:
        return "—"
    try:
        return f"{float(n) * 100:.{decimals}f} %"
    except (TypeError, ValueError):
        return "—"


def _int(n: Optional[float | int]) -> str:
    if n is None:
        return "—"
    try:
        return str(int(n))
    except (TypeError, ValueError):
        return "—"


def _str(s: Optional[str]) -> str:
    if s is None or not str(s).strip():
        return "—"
    return str(s)


def _date_fr_ca_long(d) -> str:
    """Formate une date au format français-CA long, ex: « 28 mai 2026 »."""
    if d is None:
        return "—"
    if isinstance(d, datetime):
        d = d.date()
    return f"{d.day} {_MONTHS_FR_CA[d.month - 1]} {d.year}"


def _styles(rl: Dict[str, Any]):
    PS = rl["ParagraphStyle"]
    base = rl["getSampleStyleSheet"]()
    colors = rl["colors"]
    dark = colors.HexColor(_C_DARK)
    muted = colors.HexColor(_C_MUTED)
    amber = colors.HexColor(_C_AMBER)
    return {
        "title": PS(
            "title", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=19, leading=23,
            textColor=dark, alignment=1,
        ),
        "subtitle": PS(
            "subtitle", parent=base["Normal"],
            fontName="Helvetica-Oblique", fontSize=10, leading=13,
            textColor=muted, alignment=1,
        ),
        # Titre de section : barre ambre pleine, texte blanc.
        "section": PS(
            "section", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=11.5, leading=15,
            textColor=colors.white, backColor=amber,
            leftIndent=5, rightIndent=5,
            spaceBefore=12, spaceAfter=6, borderPadding=(5, 5, 5, 5),
        ),
        "subsection": PS(
            "subsection", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=10, leading=13,
            textColor=amber, spaceBefore=7, spaceAfter=3,
        ),
        "body": PS(
            "body", parent=base["Normal"],
            fontName="Helvetica", fontSize=10, leading=13,
            textColor=dark, spaceAfter=3,
        ),
        "small": PS(
            "small", parent=base["Normal"],
            fontName="Helvetica", fontSize=9, leading=12,
            textColor=dark,
        ),
        # Variante alignée à droite pour les colonnes de chiffres.
        "num": PS(
            "num", parent=base["Normal"],
            fontName="Helvetica", fontSize=9, leading=12,
            textColor=dark, alignment=2,
        ),
        "num_b": PS(
            "num_b", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=9, leading=12,
            textColor=dark, alignment=2,
        ),
        "small_muted": PS(
            "small_muted", parent=base["Normal"],
            fontName="Helvetica", fontSize=8, leading=11,
            textColor=muted,
        ),
        # En-tête de colonne de tableau (centré, gras, ambre foncé).
        "th": PS(
            "th", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=8.5, leading=11,
            textColor=colors.HexColor(_C_AMBER), alignment=1,
        ),
        "th_left": PS(
            "th_left", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=8.5, leading=11,
            textColor=colors.HexColor(_C_AMBER), alignment=0,
        ),
        "info_ok": PS(
            "info_ok", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=10, leading=13,
            textColor=colors.HexColor(_C_GREEN),
            backColor=colors.HexColor(_C_GREEN_SOFT),
            borderColor=colors.HexColor(_C_GREEN_LINE),
            borderWidth=0.5, borderPadding=6,
            spaceBefore=4, spaceAfter=4,
        ),
        # Styles pour les tuiles de résultats clés.
        "tile_label": PS(
            "tile_label", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=7.5, leading=9,
            textColor=muted, alignment=1,
        ),
        "tile_value": PS(
            "tile_value", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=13, leading=15,
            textColor=dark, alignment=1,
        ),
        "tile_value_green": PS(
            "tile_value_green", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=13, leading=15,
            textColor=colors.HexColor(_C_GREEN), alignment=1,
        ),
        # Style pour la vedette TRI (gros chiffre vert/rouge centré).
        "tri_big": PS(
            "tri_big", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=20, leading=22,
            textColor=colors.HexColor(_C_GREEN), alignment=1,
        ),
        "tri_big_red": PS(
            "tri_big_red", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=20, leading=22,
            textColor=colors.HexColor(_C_RED), alignment=1,
        ),
        "tri_caption": PS(
            "tri_caption", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=8, leading=10,
            textColor=muted, alignment=1,
        ),
    }


def _table_two_col(rl, rows, *, s):
    """Tableau 2 colonnes (libellé / valeur) avec style cohérent. La
    valeur est alignée à droite. Accepte du markup reportlab (<b>…</b>)
    dans les valeurs."""
    Paragraph = rl["Paragraph"]
    Table = rl["Table"]
    TableStyle = rl["TableStyle"]
    mm = rl["mm"]
    colors = rl["colors"]

    data = [
        [Paragraph(str(k), s["small"]), Paragraph(str(v), s["num"])]
        for k, v in rows
    ]
    if not data:
        return None
    t = Table(data, colWidths=[78 * mm, "*"])
    t.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -2), 0.25,
             colors.HexColor(_C_LINE)),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1),
             [colors.white, colors.HexColor("#fafafa")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    return t


def _safe_json_load(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _ecart_pct(asking: Optional[float], eval_muni: Optional[float]) -> str:
    """Écart % entre prix demandé et évaluation municipale."""
    try:
        a = float(asking or 0)
        e = float(eval_muni or 0)
        if e <= 0 or a <= 0:
            return "—"
        return f"{((a - e) / e) * 100:+.1f} %"
    except Exception:  # noqa: BLE001
        return "—"


def _loyer_mois_moyen(
    revenus_bruts: Optional[float], nb_log: Optional[int]
) -> str:
    try:
        r = float(revenus_bruts or 0)
        n = int(nb_log or 0)
        if r <= 0 or n <= 0:
            return "—"
        return _money(r / 12.0 / n)
    except Exception:  # noqa: BLE001
        return "—"


def _draw_page_furniture(canvas, doc, *, address_label: str, date_label: str):
    """En-tête (filet ambre + adresse + date) et pied de page (adresse +
    numéro de page) sur chaque page."""
    canvas.saveState()
    page_w, page_h = doc.pagesize
    pt = 2.83465  # mm → pt
    left = 20 * pt
    right = page_w - 20 * pt

    # ── En-tête : filet ambre fin + libellés discrets ──────────────
    top_y = page_h - 12 * pt
    canvas.setStrokeColorRGB(0.706, 0.325, 0.035)  # ambre #b45309
    canvas.setLineWidth(1.2)
    canvas.line(left, top_y, right, top_y)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColorRGB(0.42, 0.42, 0.45)
    canvas.drawString(left, top_y + 3, address_label[:70])
    canvas.drawRightString(right, top_y + 3, date_label)

    # ── Pied de page ────────────────────────────────────────────────
    bottom_y = 10
    canvas.setFont("Helvetica", 7)
    canvas.setFillColorRGB(0.42, 0.42, 0.45)
    canvas.drawString(
        left, bottom_y,
        f"Fiche d'analyse — {address_label[:70]}",
    )
    canvas.drawRightString(right, bottom_y, f"Page {doc.page}")
    canvas.restoreState()


def _depenses_rows(
    rec: LeadAnalysis, results: Optional[dict]
) -> List[tuple]:
    """Lignes du tableau des dépenses (état actuel — sans inoccupation,
    cohérent avec ce que l'utilisateur a saisi)."""
    rows: List[tuple] = []
    rows.append(("Taxes municipales", _money(rec.taxes_municipales)))
    rows.append(("Taxes scolaires", _money(rec.taxes_scolaires)))
    rows.append(("Assurances", _money(rec.assurances)))
    rows.append(("Énergie", _money(rec.energie)))
    rows.append(("Autres dépenses", _money(rec.depenses_autres)))
    # Inoccupation : pris depuis les résultats si dispo (sinon défaut 3 %).
    inocc_pct = None
    if results:
        try:
            inocc_pct = float(results.get("taux_inoccupation_pct") or 0) * 100
        except Exception:  # noqa: BLE001
            inocc_pct = None
    if inocc_pct is None:
        inocc_pct = 3.0
    rows.append(("Taux d'inoccupation", _pct(inocc_pct, decimals=1)))
    return rows


def _key_results_band(rl, rec: LeadAnalysis, results: Optional[dict], *, s):
    """Bande de tuiles « résultats clés » en haut de la fiche : prix,
    best refi, MDF prêteur B, cashflow (achat), équité dégagée. Chaque
    tuile = un encadré arrondi avec libellé + valeur. None-safe."""
    Paragraph = rl["Paragraph"]
    Table = rl["Table"]
    TableStyle = rl["TableStyle"]
    mm = rl["mm"]
    colors = rl["colors"]

    scenarios = (results or {}).get("scenarios") or {}

    prix = rec.asking_price
    best_amount = None
    if results:
        best_amount = (results.get("best_refi") or {}).get("amount")
    if best_amount is None:
        best_amount = rec.best_refi_amount
    mdf_b = None
    if results:
        mdf_b = results.get("mdf_preteur_b")
    if mdf_b is None:
        mdf_b = rec.mdf_preteur_b
    # Scénario gagnant (best refi) : sert au cashflow ET à l'équité, pour
    # que tout le bandeau pointe sur le même scénario sélectionné.
    best = _best_refi_scenario(results) if results else None
    cashflow = None
    equite = None
    if best:
        cashflow = best.get("cashflow_annuel")
        equite = best.get("equite_a_la_fin")

    # (label, valeur, vert?) — vert pour les métriques « positives ».
    # Phase 2 : une fiche « achat direct » a ses propres métriques clés
    # (reflet du panneau de la fiche).
    _direct_band = (results or {}).get("traditionnel")
    if _direct_band and getattr(rec, "strategie_acquisition", None) in (
        "traditionnel", "conventionnel", "schl_std", "aph_50", "aph_100"
    ):
        _bb = _direct_band.get("best_refi") or {}
        _retenu = (_direct_band.get("achat") or {}).get(
            _direct_band.get("programme_retenu") or ""
        ) or {}
        tiles = [
            ("PRIX DEMANDÉ", _money(prix), False),
            (
                f"REFI AN {_direct_band.get('horizon')} (DÉGAGÉ)",
                _money(_bb.get("argent_dispo")),
                bool(_bb.get("refi_possible")),
            ),
            ("MDF (CASH À L'ACHAT)",
             _money(_direct_band.get("mdf_cash")), False),
            ("PRÊT À L'ACHAT (RETENU)",
             _money(_direct_band.get("pret_retenu")), False),
            (
                "CASHFLOW / AN",
                _money(_retenu.get("cashflow_annuel")),
                float(_retenu.get("cashflow_annuel") or 0) >= 0,
            ),
        ]
    else:
        tiles = [
        ("PRIX DEMANDÉ", _money(prix), False),
        ("BEST REFI (ÉQUITÉ)", _money(best_amount), True),
        ("MDF PRÊTEUR B", _money(mdf_b), False),
        ("CASHFLOW / AN (BEST REFI)", _money(cashflow),
         (cashflow is not None and float(cashflow or 0) >= 0)),
        ("ÉQUITÉ AU REFI", _money(equite), True),
    ]
    # Chantier stratégies (fiches avec stratégie choisie seulement —
    # PDF = reflet de la fiche) : le prêt du prêteur B en tuile, à
    # côté de la MDF.
    if (
        getattr(rec, "strategie_acquisition", None) == "preteur_b"
        and results
        and (results.get("pret_preteur_b") or {}).get("total")
    ):
        tiles.insert(
            3,
            (
                "PRÊT PRÊTEUR B",
                _money(results["pret_preteur_b"]["total"]),
                False,
            ),
        )

    cells = []
    for label, value, is_green in tiles:
        val_style = s["tile_value_green"] if is_green else s["tile_value"]
        inner = Table(
            [[Paragraph(label, s["tile_label"])],
             [Paragraph(value, val_style)]],
            colWidths=["*"],
        )
        green_bg = is_green and value != "—"
        inner.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (0, 0), 7),
                ("BOTTOMPADDING", (0, 0), (0, 0), 1),
                ("TOPPADDING", (0, 1), (0, 1), 1),
                ("BOTTOMPADDING", (0, 1), (0, 1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("BACKGROUND", (0, 0), (-1, -1),
                 colors.HexColor(_C_GREEN_SOFT if green_bg
                                 else _C_GREY_SOFT)),
                ("BOX", (0, 0), (-1, -1), 0.75,
                 colors.HexColor(_C_GREEN_LINE if green_bg
                                 else _C_LINE)),
                ("ROUNDEDCORNERS", [4, 4, 4, 4]),
            ])
        )
        cells.append(inner)

    n = len(cells)
    gap = 2.5 * mm
    total_w = 170 * mm
    col_w = (total_w - gap * (n - 1)) / n
    # Insère des colonnes-gouttières entre les tuiles.
    row = []
    widths = []
    for i, c in enumerate(cells):
        row.append(c)
        widths.append(col_w)
        if i < n - 1:
            row.append("")
            widths.append(gap)
    band = Table([row], colWidths=widths)
    band.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ])
    )
    return band


def _typology_section(rl, typology: Dict[str, int], *, s) -> Any:
    """Tableau typologie horizontal."""
    Paragraph = rl["Paragraph"]
    Table = rl["Table"]
    TableStyle = rl["TableStyle"]
    mm = rl["mm"]
    colors = rl["colors"]

    if not isinstance(typology, dict) or not typology:
        return Paragraph("Aucune typologie renseignée.", s["small_muted"])

    items = []
    total = 0
    for typo in _TYPO_ORDER:
        qty = int(typology.get(typo, 0) or 0)
        items.append((typo, qty))
        total += qty
    # Inclut les typologies hors ordre canonique éventuelles.
    extras = sorted(
        k for k in typology.keys()
        if k not in _TYPO_ORDER and int(typology.get(k, 0) or 0) > 0
    )
    for typo in extras:
        qty = int(typology.get(typo, 0) or 0)
        items.append((typo, qty))
        total += qty

    # Valeurs centrées sous les en-têtes (style dédié centré).
    val_style = rl["ParagraphStyle"](
        "typo_val", parent=s["small"], alignment=1)
    header_row = [Paragraph(f"{k}", s["th"]) for k, _ in items]
    value_row = [Paragraph(str(v), val_style) for _, v in items]
    header_row.append(Paragraph("Total", s["th"]))
    value_row.append(Paragraph(f"<b>{total}</b>", val_style))

    n = len(header_row)
    col_w = (170 / n) * mm
    t = Table([header_row, value_row], colWidths=[col_w] * n)
    t.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("BACKGROUND", (0, 0), (-1, 0),
             colors.HexColor(_C_AMBER_SOFT)),
            ("LINEBELOW", (0, 0), (-1, 0), 0.75,
             colors.HexColor(_C_AMBER_LINE)),
            ("BACKGROUND", (-1, 0), (-1, -1),
             colors.HexColor(_C_GREY_SOFT)),
            ("BOX", (0, 0), (-1, -1), 0.25,
             colors.HexColor(_C_LINE)),
            ("INNERGRID", (0, 0), (-1, -1), 0.25,
             colors.HexColor(_C_LINE)),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ])
    )
    return t


# Ordre + libellés courts des scénarios pour le tableau comparatif.
_SCENARIO_KEYS = ("achat", "refi_schl", "refi_aph_50", "refi_aph_100")
_SCENARIO_SHORT = {
    "achat": "Achat",
    "refi_schl": "Refi SCHL",
    "refi_aph_50": "Refi APH 50",
    "refi_aph_100": "Refi APH 100",
}


_TRAD_POSTE_LABELS = [
    ("courtier_hypothecaire_1", "Courtier hypothécaire (1 % × prêt)"),
    ("taxes_bienvenue", "Taxes de bienvenue (prix déclaré)"),
    ("evaluateur", "Évaluateur"),
    ("inspection", "Inspection"),
    ("avocat", "Avocat"),
    ("notaire", "Notaire"),
    ("rapport_efficacite", "Rapport efficacité énergétique"),
    ("frais_developpement", "Frais de développement"),
    ("frais_negociations", "Frais de négociations"),
    ("frais_travaux", "Frais de travaux"),
    ("frais_dossier_preteur", "Frais de dossier (montant fixe)"),
    ("interets_balance_vente", "Intérêts balance de vente (H ans)"),
    ("detention", "Détention"),
]
_TRAD_POSTES_PERMANENTS = {"interets_balance_vente", "detention"}


def _trad_composition_table(rl, trad: dict, *, s):
    """Composition de la mise de fonds — institution traditionnelle :
    même logique que le tableau de la fiche (prix déclaré − prêt
    retenu − cashback − balance de vente + frais payés cash)."""
    labels = trad.get("labels") or {}
    retenu = trad.get("programme_retenu") or ""
    prix_b = float(trad.get("prix_bancaire") or 0)
    cb = float(trad.get("cashback") or 0)
    bv = float(trad.get("balance_vente") or 0)
    pret = float(trad.get("pret_retenu") or 0)
    rows = [(
        ("Prix déclaré" if cb > 0 else "Prix d'achat")
        + f" − prêt retenu ({labels.get(retenu, retenu)})",
        _money(max(0.0, prix_b - pret)),
    )]
    if cb > 0:
        rows.append(("− Cashback reçu au notaire", _money(-cb)))
    if bv > 0:
        rows.append(("− Balance de vente (financée par le vendeur)",
                     _money(-bv)))
    fd = trad.get("frais_demarrage") or {}
    for key, lbl in _TRAD_POSTE_LABELS:
        v = float(fd.get(key) or 0)
        if abs(v) < 0.005 and key not in _TRAD_POSTES_PERMANENTS:
            continue
        rows.append((lbl, _money(v)))
    rows.append(("Frais d'acquisition (total)",
                 _money(trad.get("frais_demarrage_total"))))
    cash = trad.get("frais_demarrage_cash")
    if cash is not None and abs(float(cash) - float(
            trad.get("frais_demarrage_total") or 0)) > 0.5:
        rows.append(("dont payés cash (le reste roulé dans le prêt)",
                     _money(cash)))
    rows.append(("Total — mise de fonds (cash à l'achat)",
                 _money(trad.get("mdf_cash"))))
    return _table_two_col(rl, rows, s=s)


def _traditionnel_section(rl, trad: dict, *, s):
    """Stratégie « institution traditionnelle » — reflet des onglets de
    la fiche : sommaire d'achat (programme retenu), tableau Achat (4
    programmes), verdict et tableau Refinancement à l'an H, projection
    an 0..H."""
    Paragraph = rl["Paragraph"]
    Spacer = rl["Spacer"]

    flow = []
    best = trad.get("best_refi") or {}
    h = trad.get("horizon") or 5
    labels = trad.get("labels") or {}
    retenu_key = trad.get("programme_retenu") or "conventionnel"
    achat = trad.get("achat") or {}
    refi = trad.get("refi") or {}
    retenu = achat.get(retenu_key) or {}

    rows = [
        ("Programme retenu à l'achat",
         str(labels.get(retenu_key, retenu_key))),
        (
            "Termes du programme retenu",
            f"LTV {float(retenu.get('ltv') or 0) * 100:.0f} % · "
            f"amortissement {retenu.get('amort_annees')} ans · "
            f"RCD {float(retenu.get('rcd') or 0):.2f}",
        ),
        ("Prêt accordé à l'achat", _money(trad.get("pret_retenu"))),
        ("MDF (cash à l'achat)", _money(trad.get("mdf_cash"))),
        ("Frais d'acquisition", _money(trad.get("frais_demarrage_total"))),
        ("Total dépensé (prêt initial + cash)",
         _money(trad.get("total_depense"))),
        (
            "Croissance (loyers / dépenses)",
            f"{float(trad.get('croissance_loyers') or 0) * 100:.1f} % / "
            f"{float(trad.get('croissance_depenses') or 0) * 100:.1f} % par an",
        ),
    ]
    if float(trad.get("cashback") or 0) > 0:
        rows.append((
            "Cashback reçu au notaire",
            f"{_money(trad.get('cashback'))} · prix déclaré à "
            f"l'institution {_money(trad.get('prix_bancaire'))}",
        ))
    if float(trad.get("balance_vente") or 0) > 0:
        rows.append((
            "Balance de vente (déduite de la MDF)",
            f"{_money(trad.get('balance_vente'))} · intérêts "
            f"{_money(trad.get('interets_bv_annuels'))}/an",
        ))
    flow.append(_table_two_col(rl, rows, s=s))
    flow.append(Spacer(1, 6))

    # Tableau ACHAT — 4 programmes sur les loyers/dépenses actuels.
    mdf_par = trad.get("mdf_par_programme") or {}
    achat_rows = [
        (
            ("★ " if k == retenu_key else "") + str(labels.get(k, k)),
            f"prêt {_money((v or {}).get('financement'))} · "
            f"MDF {_money(mdf_par.get(k))} · cashflow "
            f"{_money((v or {}).get('cashflow_annuel'))}/an",
        )
        for k, v in achat.items()
    ]
    if achat_rows:
        flow.append(Paragraph("Achat — comparatif des programmes",
                              s["small_muted"]))
        flow.append(_table_two_col(rl, achat_rows, s=s))
        flow.append(Spacer(1, 6))

    # Verdict de l'an H.
    if best.get("refi_possible"):
        verdict = (
            f"À l'an {h}, le refinancement {best.get('label')} dégage "
            f"{_money(best.get('argent_dispo'))} (après remboursement du "
            "prêt d'achat et de la balance de vente) — assez pour "
            f"ressortir toute la mise de fonds de {_money(trad.get('mdf_cash'))}."
        )
    else:
        verdict = (
            f"À l'an {h}, le meilleur refinancement ({best.get('label')}) "
            f"dégage {_money(best.get('argent_dispo'))} — il manque "
            f"{_money(best.get('manque'))} pour ressortir toute la mise "
            f"de fonds de {_money(trad.get('mdf_cash'))}."
        )
    flow.append(Paragraph(verdict, s["body"]))
    flow.append(Spacer(1, 6))

    refi_rows = [
        (
            ("★ " if k == best.get("key") else "") + str(labels.get(k, k)),
            f"prêt max {_money((v or {}).get('financement'))} · argent "
            f"dégagé {_money((v or {}).get('equite_a_la_fin'))}",
        )
        for k, v in refi.items()
    ]
    if refi_rows:
        flow.append(Paragraph(
            f"Refinancement à l'an {h} — comparatif", s["small_muted"]))
        flow.append(_table_two_col(rl, refi_rows, s=s))
        flow.append(Spacer(1, 6))

    # Projection an 0..H (colonnes bornées à l'horizon pour tenir en
    # largeur — la fiche montre la série longue).
    proj = [
        p for p in (trad.get("projection") or [])
        if int(p.get("annee", 0)) <= int(h)
    ]
    if proj:
        Table = rl["Table"]
        TableStyle = rl["TableStyle"]
        mm = rl["mm"]
        colors = rl["colors"]
        header = ["Année"] + [str(p["annee"]) for p in proj]
        lignes = [
            ("Revenus", "revenus"),
            ("Dépenses", "depenses"),
            ("RNO", "rno"),
            ("Valeur", "valeur"),
            ("Solde prêt", "solde_pret"),
            ("Équité", "equite"),
        ]
        data_rows = [header] + [
            [lab] + [_money(p.get(key)) for p in proj]
            for lab, key in lignes
        ]
        col_w = [26 * mm] + [
            (144 * mm) / max(len(proj), 1) for _ in proj
        ]
        t = Table(data_rows, colWidths=col_w)
        t.setStyle(
            TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 6.5),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("TEXTCOLOR", (0, 0), (-1, -1),
                 colors.HexColor("#333333")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5,
                 colors.HexColor(_C_LINE)),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ])
        )
        flow.append(t)
    return flow


def _scenarios_table(
    rl, results: dict, *, s, winner_key: Optional[str],
    masquer_achat: bool = False,
):
    """Tableau comparatif des 4 scénarios en colonnes (un scénario par
    colonne), métriques en lignes. La colonne du scénario gagnant (best
    refi) est surlignée vert avec un badge « ★ Recommandé ». None-safe."""
    Paragraph = rl["Paragraph"]
    Table = rl["Table"]
    TableStyle = rl["TableStyle"]
    mm = rl["mm"]
    colors = rl["colors"]

    scenarios = results.get("scenarios") or {}
    present = [k for k in _SCENARIO_KEYS if scenarios.get(k)]
    if masquer_achat:
        present = [k for k in present if k != "achat"]
    if not present:
        return Paragraph(
            "Aucun scénario calculé — lance l'analyse financière "
            "depuis la fiche.",
            s["small_muted"],
        )

    def g(key, field):
        return (scenarios.get(key) or {}).get(field)

    def fmt_ltv(v):
        return f"{float(v) * 100:.0f} %" if v is not None else "—"

    def fmt_amort(v):
        return f"{int(v)} ans" if v is not None else "—"

    def fmt_rcd(v):
        return f"{float(v):.2f}" if v is not None else "—"

    # Lignes : (libellé, fonction de format).
    metric_rows = [
        ("LTV", lambda k: fmt_ltv(g(k, "ltv"))),
        ("Amortissement", lambda k: fmt_amort(g(k, "amort_annees"))),
        ("RCD", lambda k: fmt_rcd(g(k, "rcd"))),
        ("Revenus nets", lambda k: _money(g(k, "revenus_net"))),
        ("Prêt hypothécaire max", lambda k: _money(g(k, "financement"))),
        ("Valeur retenue", lambda k: _money(g(k, "valeur_retenue"))),
        ("MDF requise", lambda k: _money(g(k, "mdf_necessaire"))),
        ("Équité au refi", lambda k: _money(g(k, "equite_a_la_fin"))),
        ("Mensualité", lambda k: _money_decimal(
            g(k, "paiement_mensuel_actuel"))),
        ("Cashflow / an", lambda k: _money(g(k, "cashflow_annuel"))),
    ]

    # En-tête : coin vide + un libellé par scénario (badge si gagnant).
    header = [Paragraph("", s["th_left"])]
    for k in present:
        label = _SCENARIO_SHORT.get(k, k)
        if k == winner_key:
            header.append(Paragraph(
                f"{label}<br/><font size=6 color='{_C_GREEN}'>"
                f"★ RECOMMANDÉ</font>", s["th"]))
        else:
            header.append(Paragraph(label, s["th"]))

    data = [header]
    for label, fn in metric_rows:
        row = [Paragraph(label, s["small"])]
        for k in present:
            row.append(Paragraph(fn(k), s["num"]))
        data.append(row)

    n_cols = len(present) + 1
    first_w = 42 * mm
    other_w = (170 * mm - first_w) / len(present)
    col_widths = [first_w] + [other_w] * len(present)
    t = Table(data, colWidths=col_widths, repeatRows=1)

    style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(_C_AMBER_SOFT)),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75,
         colors.HexColor(_C_AMBER_LINE)),
        ("BACKGROUND", (0, 1), (0, -1), colors.HexColor(_C_GREY_SOFT)),
        ("ROWBACKGROUNDS", (1, 1), (-1, -1),
         [colors.white, colors.HexColor("#fafafa")]),
        ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor(_C_LINE)),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor(_C_LINE)),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    # Surlignage de la colonne gagnante.
    if winner_key in present:
        col = present.index(winner_key) + 1
        style.append(("BACKGROUND", (col, 1), (col, -1),
                      colors.HexColor(_C_GREEN_SOFT)))
        style.append(("BACKGROUND", (col, 0), (col, 0),
                      colors.HexColor(_C_GREEN_SOFT)))
        style.append(("BOX", (col, 0), (col, -1), 1.2,
                      colors.HexColor(_C_GREEN_LINE)))
    t.setStyle(TableStyle(style))
    return t


def _scenario_card(
    rl, scen: Optional[dict], *, s, frais_demarrage_total: float
):
    """Encart vertical détaillé d'un scénario (conservé pour compat).

    Le PDF utilise désormais `_scenarios_table` pour la vue comparative,
    mais ce helper reste disponible et None-safe."""
    Paragraph = rl["Paragraph"]
    KeepTogether = rl["KeepTogether"]
    Spacer = rl["Spacer"]

    if not scen:
        return Paragraph("Scénario non calculé.", s["small_muted"])

    ltv = scen.get("ltv")
    amort = scen.get("amort_annees")
    rcd = scen.get("rcd")
    revenus_net = scen.get("revenus_net")
    financement = scen.get("financement")
    paiement = scen.get("paiement_mensuel_actuel")
    cashflow = scen.get("cashflow_annuel")
    mdf_nec = scen.get("mdf_necessaire")
    equite = scen.get("equite_a_la_fin")

    surplus_mois = None
    try:
        if revenus_net is not None and paiement is not None:
            surplus_mois = float(revenus_net) / 12.0 - float(paiement)
    except Exception:  # noqa: BLE001
        surplus_mois = None

    fonds_necessaires = None
    if mdf_nec is not None:
        try:
            fonds_necessaires = float(mdf_nec) + float(
                frais_demarrage_total or 0
            )
        except Exception:  # noqa: BLE001
            fonds_necessaires = None

    rows = [
        ("LTV",
         f"{float(ltv) * 100:.0f} %" if ltv is not None else "—"),
        ("Amortissement",
         f"{int(amort)} ans" if amort is not None else "—"),
        ("RCD", f"{float(rcd):.2f}" if rcd is not None else "—"),
        ("Revenus nets", _money(revenus_net)),
        ("Prêt hypothécaire max", _money(financement)),
    ]
    if mdf_nec is not None:
        rows.append(("MDF requise", _money(mdf_nec)))
        if fonds_necessaires is not None:
            rows.append(
                ("Fonds nécessaires (MDF + frais démarrage)",
                 _money(fonds_necessaires))
            )
    if equite is not None:
        rows.append(("Équité dégagée au refi", _money(equite)))
    rows.append(("Mensualité hypothécaire",
                 _money_decimal(paiement)))
    if surplus_mois is not None:
        rows.append(("Surplus mensuel",
                     _money_decimal(surplus_mois)))
    if cashflow is not None:
        rows.append(("Cashflow annuel", _money(cashflow)))

    label = scen.get("label") or scen.get("name") or "Scénario"
    title = Paragraph(f"<b>{label}</b>", s["subsection"])
    table = _table_two_col(rl, rows, s=s)
    return KeepTogether([title, table or Spacer(1, 4), Spacer(1, 4)])


def _format_warning_severity(sev: Optional[str]) -> str:
    return {
        "error": "Erreur",
        "warning": "Avertissement",
        "info": "Info",
    }.get((sev or "").lower(), "Info")


def _best_refi_scenario(results: Optional[dict]) -> Optional[dict]:
    """Retrouve le scénario refi gagnant dans `analysis_results_json`.

    Dupliqué de `lead_analyses._best_refi_scenario` pour garder ce
    service autonome (cloud-only). On matche d'abord par
    `best_refi.program` (= `label` du scénario gagnant) ; à défaut, on
    prend le scénario refi avec la plus grande `equite_a_la_fin`."""
    scenarios = (results or {}).get("scenarios") or {}
    refis = [
        s
        for k, s in scenarios.items()
        if k.startswith("refi_") and isinstance(s, dict)
    ]
    if not refis:
        return None
    best = ((results or {}).get("best_refi") or {}).get("program")
    if best:
        for s in refis:
            if s.get("label") == best:
                return s
    return max(refis, key=lambda s: s.get("equite_a_la_fin") or 0.0)


def _best_refi_key(results: Optional[dict]) -> Optional[str]:
    """Clé (`refi_schl` / `refi_aph_50` / `refi_aph_100`) du scénario
    gagnant, pour surligner la bonne colonne du tableau comparatif."""
    win = _best_refi_scenario(results)
    if win is None:
        return None
    scenarios = (results or {}).get("scenarios") or {}
    for k, sc in scenarios.items():
        if sc is win:
            return k
    # Fallback : match par label.
    label = win.get("label")
    for k, sc in scenarios.items():
        if isinstance(sc, dict) and sc.get("label") == label:
            return k
    return None


def _compute_rci_pvi(
    rec: LeadAnalysis, results: Optional[dict]
) -> tuple[Optional[float], Optional[float]]:
    """RCI = équité refi / MDF achat (% capital récupéré).
       PVI = financement refi - prix d'acquisition (prise de valeur)."""
    if not results:
        return None, None
    scenarios = results.get("scenarios") or {}
    chosen = _best_refi_scenario(results)

    achat = scenarios.get("achat") or {}
    mdf_achat = achat.get("mdf_necessaire")
    prix_acq = results.get("prix_acquisition")

    rci = None
    pvi = None
    if chosen is not None:
        equite = chosen.get("equite_a_la_fin")
        financement = chosen.get("financement")
        try:
            if mdf_achat and float(mdf_achat) > 0 and equite is not None:
                rci = (float(equite) / float(mdf_achat)) * 100.0
        except Exception:  # noqa: BLE001
            rci = None
        try:
            if financement is not None and prix_acq is not None:
                pvi = float(financement) - float(prix_acq)
        except Exception:  # noqa: BLE001
            pvi = None
    return rci, pvi


# ── TRI : dérivation des intrants (dupliquée du backend) ─────────────
#
# Cf. `lead_analyses._derive_tri_auto_inputs` / `_persisted_manual_inputs`.
# On duplique ici la logique pour garder ce service de rendu autonome
# (cloud-only, pas d'import circulaire endpoint → service). Mapping AUTO :
#   prix        = prix_achat
#   rpv_achat   = 1 − mdf_preteur_b_pct
#   pret_constr = Σ frais_financables × (1 − mdf_pct)
#   mdf         = mdf_preteur_b
#   loyers2     = best_refi.revenus_totaux
#   dep2        = best_refi.depenses_total
#   valeur2     = best_refi.valeur_retenue
#   rpv_refi    = best_refi.ltv

def _derive_tri_auto_inputs(results: dict) -> dict:
    """Dérive les 8 intrants AUTO depuis `analysis_results_json`
    (miroir de l'endpoint /tri-inputs). Depuis 2026-09-04 les
    deux stratégies sont couvertes : en institution traditionnelle,
    la dette initiale = prêt retenu + balance de vente, la MDF = cash à
    l'achat et le refi de référence est celui de l'onglet
    Refinancement. En prêteur B, le ratio prêt-valeur se lit sur le
    prêt réellement accordé (cashback → plus gros prêt sur le prix
    réel) + balance de vente. Retourne un dict des 8 clés auto."""
    prix = float(results.get("prix_achat") or 0)

    trad = results.get("traditionnel")
    if isinstance(trad, dict) and isinstance(trad.get("refi"), dict):
        best_key = (trad.get("best_refi") or {}).get("key")
        best = (trad.get("refi") or {}).get(best_key) or {}
        dette = float(trad.get("pret_retenu") or 0) + float(
            trad.get("balance_vente") or 0
        )
        total = float(trad.get("frais_demarrage_total") or 0)
        cash = trad.get("frais_demarrage_cash")
        pret_constr = (
            max(0.0, total - float(cash)) if cash is not None else 0.0
        )
        return {
            "prix": prix,
            "rpv_achat": (dette / prix) if prix > 0 else 0.0,
            # Frais roulés dans le prêt de l'institution (postes cochés).
            "pret_constr": pret_constr,
            "mdf": float(trad.get("mdf_cash") or 0),
            "loyers2": float(best.get("revenus_totaux") or 0),
            "dep2": float(best.get("depenses_total") or 0),
            "valeur2": float(best.get("valeur_retenue") or 0),
            "rpv_refi": float(best.get("ltv") or 0),
        }

    mdf_pct = float(results.get("mdf_preteur_b_pct") or 0)
    mdf = float(results.get("mdf_preteur_b") or 0)

    # pret_constr = portion des frais finançables prise en charge par le
    # prêteur B = Σ frais[k] × (1 − mdf_pct) sur les postes finançables.
    frais = results.get("frais_demarrage") or {}
    financables = set(results.get("frais_demarrage_financables") or [])
    pret_constr = 0.0
    for k, v in frais.items():
        if k == "frais_custom":
            continue  # liste de postes perso — traités juste après
        if k in financables:
            try:
                pret_constr += float(v or 0) * (1.0 - mdf_pct)
            except (TypeError, ValueError):
                continue
    # Postes personnalisés finançables (même logique).
    for c in (frais.get("frais_custom") or []):
        if isinstance(c, dict) and c.get("financable"):
            try:
                pret_constr += float(c.get("montant") or 0) * (1.0 - mdf_pct)
            except (TypeError, ValueError):
                continue

    # Dette initiale = prêt B sur le prix DÉCLARÉ + balance de vente ;
    # sans cashback ni BV = (1 − mdf_pct) × prix, comme avant.
    pret_b = (results.get("pret_preteur_b") or {}).get("sur_prix")
    bv = float((results.get("balance_vente") or {}).get("montant") or 0)
    if pret_b is not None and prix > 0:
        rpv_achat = (float(pret_b) + bv) / prix
    else:
        rpv_achat = max(0.0, 1.0 - mdf_pct)

    # Scénario refi GAGNANT (best refi) — matché par
    # ``best_refi.program == scenario.label``. Les revenus, dépenses,
    # valeur et LTV des intrants TRI proviennent TOUS de ce même
    # scénario : les scénarios APH (50/100) ont des revenus/dépenses
    # différents (thermopompes, split abordable/PDM), donc on ne doit
    # PAS prendre un scénario générique. Cohérent avec ``valeur2`` et
    # ``rpv_refi`` (déjà issus du best refi).
    best = _best_refi_scenario(results) or {}
    return {
        "prix": prix,
        "rpv_achat": rpv_achat,
        "pret_constr": pret_constr,
        "mdf": mdf,
        # loyers2 / dep2 = revenus & dépenses d'opération DU best refi
        # (mêmes que valeur2 / rpv_refi ci-dessous → cohérence garantie).
        "loyers2": float(best.get("revenus_totaux") or 0),
        "dep2": float(best.get("depenses_total") or 0),
        "valeur2": float(best.get("valeur_retenue") or 0),
        "rpv_refi": float(best.get("ltv") or 0),
    }


def _annee_refi_de(rec) -> int:
    """Année du refinancement de la fiche (retour Phil 2026-09-04) :
    durée du projet en prêteur B, horizon de détention en institution
    traditionnelle ; 2 (Excel) tant que la fiche n'a pas de stratégie."""
    strat = getattr(rec, "strategie_acquisition", None)
    if not strat:
        return 2
    if strat == "preteur_b":
        return max(1, int(getattr(rec, "duree_projet_annees", None) or 2))
    return max(1, int(getattr(rec, "projection_horizon_annees", None) or 5))


def _persisted_manual_inputs(rec: LeadAnalysis) -> dict:
    """Lit les 4 intrants manuels persistés sur la fiche, ou défauts en
    dur (`_TRI_DEFAULTS`). Pour le PDF on n'a pas accès aux défauts BD
    configurables (`tri_defaults`) sans requête supplémentaire — on
    retombe donc sur les défauts en dur, ce qui reste cohérent avec le
    comportement de l'endpoint quand le groupe BD est vide."""
    cap = (
        float(rec.tri_capital_injecte)
        if rec.tri_capital_injecte is not None
        else _TRI_DEFAULTS["capital"]
    )
    pct = (
        float(rec.tri_pct_investisseur)
        if rec.tri_pct_investisseur is not None
        else _TRI_DEFAULTS["pct"]
    )
    cr_l = (
        float(rec.tri_croissance_loyers)
        if rec.tri_croissance_loyers is not None
        else _TRI_DEFAULTS["cr_loyers"]
    )
    cr_d = (
        float(rec.tri_croissance_depenses)
        if rec.tri_croissance_depenses is not None
        else _TRI_DEFAULTS["cr_dep"]
    )
    return {"capital": cap, "pct": pct, "cr_loyers": cr_l, "cr_dep": cr_d}


def _tri_section(rl, rec: LeadAnalysis, results: Optional[dict], *, s):
    """Construit la section « Rendement de l'investisseur (TRI) ».

    Retourne une liste de flowables. Si les intrants TRI ne sont pas
    disponibles (capital non renseigné OU analyse financière absente),
    affiche un message « TRI non calculé » plutôt que de planter.
    Entièrement None-safe : tout échec de calcul retombe sur le message
    d'omission propre."""
    Paragraph = rl["Paragraph"]
    Table = rl["Table"]
    TableStyle = rl["TableStyle"]
    Spacer = rl["Spacer"]
    mm = rl["mm"]
    colors = rl["colors"]

    out: List[Any] = [Paragraph(
        "RENDEMENT DE L'INVESTISSEUR (TRI)", s["section"])]

    # Condition minimale : capital injecté renseigné sur la fiche.
    if rec.tri_capital_injecte is None:
        out.append(Paragraph(
            "TRI non calculé — renseigne le « capital injecté » de "
            "l'investisseur sur la fiche pour générer le rendement.",
            s["small_muted"]))
        return out
    if not results:
        out.append(Paragraph(
            "TRI non calculé — lance d'abord l'analyse financière "
            "(les intrants automatiques en dépendent).",
            s["small_muted"]))
        return out

    # Dérivation + calcul, entièrement défensifs.
    try:
        from app.services.lead_tri_calc import compute_tri  # type: ignore
        auto = _derive_tri_auto_inputs(results)
        manual = _persisted_manual_inputs(rec)
        tri_data = compute_tri(
            prix=auto["prix"],
            rpv_achat=auto["rpv_achat"],
            pret_constr=auto["pret_constr"],
            mdf=auto["mdf"],
            capital=manual["capital"] or 0.0,
            pct=manual["pct"],
            loyers2=auto["loyers2"],
            dep2=auto["dep2"],
            valeur2=auto["valeur2"],
            rpv_refi=auto["rpv_refi"],
            cr_loyers=manual["cr_loyers"],
            cr_dep=manual["cr_dep"],
            annee_refi=_annee_refi_de(rec),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Section TRI PDF — calcul échoué : %s", exc)
        out.append(Paragraph(
            "TRI non calculé — données insuffisantes pour le moteur "
            "de rendement.", s["small_muted"]))
        return out

    tri = tri_data.get("tri") or {}
    sommaire = tri_data.get("sommaire") or {}
    horizons = tri_data.get("horizons") or {}
    flux = tri_data.get("flux") or {}

    # Rappel des hypothèses (capital, parts, croissances).
    out.append(Paragraph(
        f"Capital injecté <b>{_money(manual['capital'])}</b> · "
        f"parts investisseur <b>{_pct_fraction(manual['pct'], 0)}</b> · "
        f"croissance loyers <b>{_pct_fraction(manual['cr_loyers'])}</b> · "
        f"croissance dépenses <b>{_pct_fraction(manual['cr_dep'])}</b>",
        s["small_muted"]))
    out.append(Spacer(1, 6))

    # ── Les 3 TRI en vedette (sortie an 2 / 7 / 12) ──────────────────
    def _tri_cell(label_h, value):
        big = s["tri_big"]
        if value is None:
            disp = "—"
        else:
            disp = f"{float(value) * 100:.1f} %"
            if float(value) < 0:
                big = s["tri_big_red"]
        inner = Table(
            [[Paragraph(disp, big)],
             [Paragraph(label_h, s["tri_caption"])]],
            colWidths=["*"])
        inner.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(_C_GREY_SOFT)),
            ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor(_C_AMBER_LINE)),
            ("ROUNDEDCORNERS", [5, 5, 5, 5]),
            ("TOPPADDING", (0, 0), (0, 0), 9),
            ("BOTTOMPADDING", (0, 1), (0, 1), 9),
            ("TOPPADDING", (0, 1), (0, 1), 0),
            ("BOTTOMPADDING", (0, 0), (0, 0), 0),
        ]))
        return inner

    hz = [int(x) for x in (tri_data.get("horizons_list") or [2, 7, 12])]
    c2, c7, c12 = [
        _tri_cell(f"Sortie an {h}", tri.get(f"an{h}")) for h in hz
    ]
    gap = 4 * mm
    col_w = (170 * mm - 2 * gap) / 3
    vedette = Table(
        [[c2, "", c7, "", c12]],
        colWidths=[col_w, gap, col_w, gap, col_w])
    vedette.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    out.append(vedette)
    out.append(Spacer(1, 8))

    # ── Cash retourné / valeur des parts par horizon ─────────────────
    out.append(Paragraph(
        "Cash encaissé et valeur des parts par horizon", s["subsection"]))
    header = [
        Paragraph("Horizon", s["th_left"]),
        Paragraph("Valeur immeuble", s["th"]),
        Paragraph("Prêt max refi", s["th"]),
        Paragraph("Cash investisseur", s["th"]),
        Paragraph("Valeur des parts", s["th"]),
        Paragraph("Patrimoine", s["th"]),
    ]
    rows = [header]
    for h in [str(x) for x in hz]:
        hd = horizons.get(h) or {}
        cash_inv = hd.get("cash_investisseur")
        val_parts = hd.get("valeur_parts")
        # Patrimoine de l'investisseur à cet horizon = liquidités
        # encaissées cette année + valeur de ses parts. None-safe :
        # une absence de l'un OU l'autre intrant → « — ».
        if cash_inv is None and val_parts is None:
            patrimoine = None
        else:
            patrimoine = float(cash_inv or 0) + float(val_parts or 0)
        rows.append([
            Paragraph(f"An {h}", s["small"]),
            Paragraph(_money(hd.get("valeur_immeuble")), s["num"]),
            Paragraph(_money(hd.get("pret_max_refi")), s["num"]),
            Paragraph(_money(cash_inv), s["num"]),
            Paragraph(_money(val_parts), s["num"]),
            Paragraph(f"<b>{_money(patrimoine)}</b>", s["num_b"]),
        ])
    th = Table(
        rows, colWidths=[20 * mm, "*", "*", "*", "*", "*"], repeatRows=1)
    th.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(_C_AMBER_SOFT)),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor(_C_AMBER_LINE)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#fafafa")]),
        # Colonne Patrimoine mise en relief (fond vert pâle).
        ("BACKGROUND", (-1, 1), (-1, -1), colors.HexColor(_C_GREEN_SOFT)),
        ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor(_C_LINE)),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor(_C_LINE)),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    out.append(th)
    out.append(Spacer(1, 4))
    out.append(Paragraph(
        f"Total cash encaissé (hors vente) sur {hz[-1]} ans : "
        f"<b>{_money(sommaire.get('total_cash_sans_vente'))}</b>",
        s["small"]))

    # ── Ligne de temps des flux (scénario de sortie an 12) ───────────
    flux12 = flux.get(str(hz[-1]))
    if isinstance(flux12, list) and any(abs(float(x or 0)) > 0.5
                                        for x in flux12):
        out.append(Spacer(1, 8))
        out.append(Paragraph(
            f"Ligne de temps des flux — sortie an {hz[-1]}", s["subsection"]))
        # On affiche uniquement les années avec un flux non nul + l'an 0.
        years = [i for i, v in enumerate(flux12)
                 if i == 0 or abs(float(v or 0)) > 0.5]
        head = [Paragraph("Année", s["th_left"])]
        vals = [Paragraph("Flux net", s["small"])]
        for i in years:
            head.append(Paragraph(f"An {i}", s["th"]))
            v = float(flux12[i] or 0)
            vals.append(Paragraph(_money(v), s["num"]))
        n = len(head)
        first_w = 24 * mm
        other_w = (170 * mm - first_w) / (n - 1)
        ft = Table([head, vals],
                   colWidths=[first_w] + [other_w] * (n - 1))
        ft.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(_C_AMBER_SOFT)),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5,
             colors.HexColor(_C_AMBER_LINE)),
            ("BACKGROUND", (0, 1), (0, 1), colors.HexColor(_C_GREY_SOFT)),
            ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor(_C_LINE)),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor(_C_LINE)),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        out.append(ft)
        out.append(Paragraph(
            "An 0 = capital injecté (sortie de fonds) ; années "
            "suivantes = cash encaissé + liquidation des parts à la "
            "sortie.", s["small_muted"]))

    return out


async def _load(
    db: AsyncSession, analysis_id: int
) -> tuple[Optional[LeadAnalysis], List[LeadAnalysisAttachment]]:
    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        return None, []
    atts = list(
        (
            await db.execute(
                select(LeadAnalysisAttachment)
                .where(
                    LeadAnalysisAttachment.lead_analysis_id == analysis_id
                )
                .order_by(LeadAnalysisAttachment.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return rec, atts


def _render_bytes(
    rec: LeadAnalysis,
    attachments: List[LeadAnalysisAttachment],
) -> bytes:
    """Rend le PDF complet d'une `LeadAnalysis`."""
    rl = _lazy_reportlab()
    Paragraph = rl["Paragraph"]
    Spacer = rl["Spacer"]
    Table = rl["Table"]
    TableStyle = rl["TableStyle"]
    Image = rl["Image"]
    mm = rl["mm"]
    colors = rl["colors"]

    buf = io.BytesIO()
    address_label = (rec.address or f"Lead #{rec.id}").strip() or f"Lead #{rec.id}"
    date_label = _date_fr_ca_long(datetime.utcnow())
    doc = rl["SimpleDocTemplate"](
        buf,
        pagesize=rl["A4"],
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=22 * mm,
        bottomMargin=20 * mm,
        title=f"Fiche d'analyse {rec.id}",
        author="MGV Développement",
    )
    s = _styles(rl)
    story: list = []

    # ── En-tête ─────────────────────────────────────────────────
    if os.path.exists(_LOGO_PATH):
        try:
            header_logo = Image(_LOGO_PATH, width=20 * mm, height=20 * mm)
            header = Table(
                [[
                    header_logo,
                    Paragraph(
                        "<b>MGV Développement</b><br/>"
                        f"<font size=7 color='{_C_MUTED}'>"
                        "Pôle Prospection</font>",
                        s["small"],
                    ),
                ]],
                colWidths=[24 * mm, "*"],
            )
            header.setStyle(
                TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")])
            )
            story.append(header)
            story.append(Spacer(1, 6))
        except Exception:  # noqa: BLE001
            pass

    story.append(Paragraph(
        "FICHE D'ANALYSE D'OPPORTUNITÉ", s["title"]
    ))
    story.append(Paragraph(
        f"{address_label} · générée le {date_label}",
        s["subtitle"],
    ))
    story.append(Spacer(1, 10))

    results = _safe_json_load(rec.analysis_results_json)

    # ── Bande de résultats clés (tuiles) ────────────────────────
    story.append(_key_results_band(rl, rec, results, s=s))
    story.append(Spacer(1, 10))

    # ── Identité de l'immeuble ──────────────────────────────────
    identity_rows: List[tuple] = []
    addr_full_parts = [rec.address, rec.city, rec.postal_code]
    addr_full = ", ".join(p for p in addr_full_parts if p and p.strip())
    identity_rows.append(("Adresse complète", _str(addr_full)))
    identity_rows.append(("Province", _str(rec.province)))
    identity_rows.append(("Type de bâtiment", _str(rec.type_batiment)))
    identity_rows.append(
        ("Année de construction", _int(rec.annee_construction))
    )
    identity_rows.append(("Nombre de logements", _int(rec.nb_logements)))
    identity_rows.append(
        ("Stationnements", _int(rec.nb_stationnements))
    )
    if rec.superficie_batiment:
        identity_rows.append(
            ("Superficie bâtiment", f"{rec.superficie_batiment} pi²")
        )
    if rec.superficie_terrain:
        identity_rows.append(
            ("Superficie terrain", f"{rec.superficie_terrain} pi²")
        )
    identity_rows.append(("Courtier", _str(rec.courtier_nom)))
    identity_rows.append(
        ("Contact courtier", _str(rec.courtier_contact))
    )
    identity_rows.append(("Source — Extracteur", _str(rec.model_used)))
    first_url = None
    if rec.source_urls:
        for line in rec.source_urls.splitlines():
            line = line.strip()
            if line:
                first_url = line
                break
    identity_rows.append(("Source — URL principale", _str(first_url)))

    story.append(Paragraph("IDENTITÉ DE L'IMMEUBLE", s["section"]))
    t = _table_two_col(rl, identity_rows, s=s)
    if t is not None:
        story.append(t)
    story.append(Spacer(1, 4))

    # ── Informations financières — état actuel ─────────────────
    story.append(Paragraph(
        "INFORMATIONS FINANCIÈRES — ÉTAT ACTUEL", s["section"]
    ))
    fin_rows: List[tuple] = []
    fin_rows.append(("Prix demandé", _money(rec.asking_price)))
    fin_rows.append(
        ("Évaluation municipale", _money(rec.evaluation_municipale))
    )
    fin_rows.append(
        ("Écart prix / évaluation",
         _ecart_pct(
             float(rec.asking_price) if rec.asking_price else None,
             float(rec.evaluation_municipale)
             if rec.evaluation_municipale else None,
         ))
    )
    fin_rows.append(("Revenus bruts annuels", _money(rec.revenus_bruts)))
    fin_rows.append(
        ("Loyer moyen mensuel (calculé)",
         _loyer_mois_moyen(
             float(rec.revenus_bruts) if rec.revenus_bruts else None,
             rec.nb_logements,
         ))
    )
    for label, val in _depenses_rows(rec, results):
        fin_rows.append((label, val))
    # NOI et cap rate calculés à partir du scénario achat si dispo.
    if results:
        scen_achat = (results.get("scenarios") or {}).get("achat") or {}
        revenus_net = scen_achat.get("revenus_net")
        if revenus_net is not None:
            fin_rows.append(
                ("Revenus nets (NOI — scénario achat)",
                 _money(revenus_net))
            )
        try:
            if (
                revenus_net is not None
                and rec.asking_price
                and float(rec.asking_price) > 0
            ):
                cap = float(revenus_net) / float(rec.asking_price) * 100.0
                fin_rows.append(
                    ("Cap rate (sur prix demandé)",
                     _pct(cap, decimals=2))
                )
        except Exception:  # noqa: BLE001
            pass

    t = _table_two_col(rl, fin_rows, s=s)
    if t is not None:
        story.append(t)

    # ── Typologie des logements ─────────────────────────────────
    story.append(Paragraph(
        "TYPOLOGIE DES LOGEMENTS", s["section"]
    ))
    typology = _safe_json_load(rec.typology_json) or {}
    story.append(_typology_section(rl, typology, s=s))
    story.append(Spacer(1, 6))

    # ── Inputs manuels d'analyse ───────────────────────────────
    story.append(Paragraph(
        "INPUTS MANUELS D'ANALYSE", s["section"]
    ))
    inputs_rows: List[tuple] = [
        ("Taux d'intérêt refi",
         _pct(rec.taux_interet_refi_pct, decimals=3)),
        ("Taux d'intérêt prêteur B (pendant projet)",
         _pct(rec.taux_interet_preteur_b_projet_pct, decimals=3)),
        ("% MDF prêteur B",
         _pct(rec.mdf_preteur_b_pct, decimals=2)),
        ("TGA (taux global d'actualisation)",
         _pct(rec.tga_pct, decimals=3)),
        ("Taux d'intérêt achat",
         _pct(rec.taux_interet_achat_pct, decimals=3)),
        ("Durée du projet",
         f"{rec.duree_projet_annees} ans"
         if rec.duree_projet_annees else "—"),
        ("Réduction énergie",
         _pct(rec.reduction_energie_pct, decimals=1)),
        ("Nb logements ajoutés", _int(rec.nb_logements_ajoutes)),
        ("Nb thermopompes ajoutées",
         _int(rec.nb_thermopompes_ajoutees)),
        ("Ajout WiFi",
         "Oui" if rec.ajout_wifi else
         ("Non" if rec.ajout_wifi is False else "—")),
        ("Frais de développement", _money(rec.frais_developpement)),
        ("Frais de négociations", _money(rec.frais_negociations)),
        ("Travaux estimés", _money(rec.travaux_estimes)),
    ]
    # Inoccupation (depuis résultats si dispo).
    if results and results.get("taux_inoccupation_pct") is not None:
        try:
            inocc = float(results.get("taux_inoccupation_pct")) * 100
            inputs_rows.append(
                ("Taux d'inoccupation", _pct(inocc, decimals=1))
            )
        except Exception:  # noqa: BLE001
            pass
    t = _table_two_col(rl, inputs_rows, s=s)
    if t is not None:
        story.append(t)

    # ── Composition de la mise de fonds ─────────────────────────
    # Institution traditionnelle : reflet du tableau de l'onglet
    # Analyse (prix déclaré − prêt retenu − cashback − BV + frais).
    _trad_comp = (
        (results or {}).get("traditionnel")
        if getattr(rec, "strategie_acquisition", None) in (
            "traditionnel", "conventionnel", "schl_std",
            "aph_50", "aph_100",
        )
        else None
    )
    if _trad_comp:
        story.append(Paragraph(
            "COMPOSITION DE LA MISE DE FONDS — INSTITUTION "
            "TRADITIONNELLE", s["section"]
        ))
        story.append(_trad_composition_table(rl, _trad_comp, s=s))
    else:
        story.append(Paragraph(
            "COMPOSITION MDF PRÊTEUR B", s["section"]
        ))
    if results and not _trad_comp:
        fd = results.get("frais_demarrage") or {}
        fd_total = float(results.get("frais_demarrage_total") or 0)
        mdf_pct_amt = float(results.get("mdf_pct_prix_achat") or 0)
        mdf_total = float(results.get("mdf_preteur_b") or 0)
        financables = set(results.get("frais_demarrage_financables") or [])
        # Fraction payée cash sur un poste finançable (le reste est prêté
        # par le prêteur B). Cohérent avec le moteur finance : pour un
        # poste finançable on sort `mdf_pct` en cash et on finance le
        # complément `1 − mdf_pct`.
        mdf_pct = float(results.get("mdf_preteur_b_pct") or 0)

        # (clé, libellé) — on annote « (finançable) » les postes pris en
        # charge (partiellement) par le prêteur B. Sert aussi de table de
        # libellés par défaut quand un poste FIXE est piloté par le registre.
        poste_defs = [
            ("evaluateur", "Évaluateur 1"),
            ("evaluateur_2", "Évaluateur 2"),
            ("inspection", "Inspection"),
            ("avocat", "Avocat"),
            ("notaire", "Notaire 1"),
            ("notaire_2", "Notaire 2"),
            ("rapport_efficacite", "Rapport efficacité énergétique"),
            ("courtier_hypothecaire_1", "Courtier hypothécaire 1"),
            ("courtier_hypothecaire_2", "Courtier hypothécaire 2"),
            ("taxes_bienvenue", "Taxes de bienvenue (calculées)"),
            ("frais_developpement", "Frais de développement"),
            ("frais_negociations", "Frais de négociations"),
            ("frais_travaux", "Frais de travaux"),
            ("frais_dossier_preteur", "Frais de dossier du prêteur"),
            ("interets", "Intérêts pendant projet (portage)"),
            ("revenus_nets_pendant_projet",
             "Revenus nets pendant projet"),
            ("interets_balance_vente", "Intérêts balance de vente"),
            ("detention", "Détention"),
        ]
        _fixed_label_by_key = {k: lbl for k, lbl in poste_defs}

        # Registre unifié des frais de démarrage : liste ORDONNÉE
        # [{key, label_fr, visible}] injectée par le moteur dans les
        # résultats. `key` = clé d'un poste FIXE OU `id` d'un poste
        # PERSONNALISÉ. Absent (anciens résultats non recalculés) → None →
        # fallback sur `poste_defs` + sous-section custom (ordre historique).
        registry = results.get("frais_registry")
        if not isinstance(registry, list):
            registry = None

        # Postes personnalisés indexés par id (pour résolution via registre).
        custom_by_id = {}
        for c in (fd.get("frais_custom") or []):
            if isinstance(c, dict):
                cid = str(c.get("id") or "")
                if cid:
                    custom_by_id[cid] = c

        def _poste_split(key: str) -> tuple:
            """(valeur, cash_à_sortir, prêt_prêteur_B) pour un poste FIXE.

            Poste finançable → cash = valeur × mdf_pct, prêt = le
            complément. Sinon 100 % cash, prêt nul. None-safe."""
            try:
                valeur = float(fd.get(key) or 0)
            except (TypeError, ValueError):
                valeur = 0.0
            if key in financables:
                cash = valeur * mdf_pct
                pret = valeur - cash
            else:
                cash = valeur
                pret = 0.0
            return valeur, cash, pret

        def _custom_split(c: dict) -> tuple:
            """(cash, prêt) pour un poste PERSONNALISÉ. `financable` est
            déjà l'état effectif PAR FICHE (calculé par le moteur)."""
            try:
                cval = float(c.get("montant") or 0)
            except (TypeError, ValueError):
                cval = 0.0
            if bool(c.get("financable")):
                ccash = cval * mdf_pct
                cpret = cval - ccash
            else:
                ccash = cval
                cpret = 0.0
            return ccash, cpret

        # En-tête à 3 colonnes de chiffres : valeur du poste, cash à
        # sortir (MDF) et portion financée par le prêteur B.
        header = [
            Paragraph("Poste", s["th_left"]),
            Paragraph("Cash à sortir", s["th"]),
            Paragraph("Prêt prêteur B", s["th"]),
        ]
        data_rows: List[list] = [header]
        total_cash_finances = 0.0

        def _append_fixed(key: str, label: str) -> None:
            """Ajoute une ligne de poste FIXE (libellé fourni par le
            registre ou la table par défaut)."""
            nonlocal total_cash_finances
            tag = (f" <font size=7 color='{_C_GREEN}'>(finançable)</font>"
                   if key in financables else "")
            _valeur, cash, pret = _poste_split(key)
            total_cash_finances += pret
            data_rows.append([
                Paragraph(f"{label}{tag}", s["small"]),
                Paragraph(_money(cash), s["num"]),
                Paragraph(_money(pret) if pret > 0.5 else "—", s["num"]),
            ])

        def _append_custom(c: dict, label: str) -> None:
            """Ajoute une ligne de poste PERSONNALISÉ (affiché même à 0 $)."""
            nonlocal total_cash_finances
            cfin = bool(c.get("financable"))
            ccash, cpret = _custom_split(c)
            total_cash_finances += cpret
            ctag = (f" <font size=7 color='{_C_GREEN}'>(finançable)</font>"
                    if cfin else "")
            data_rows.append([
                Paragraph(f"{label}{ctag}", s["small"]),
                Paragraph(_money(ccash), s["num"]),
                Paragraph(_money(cpret) if cpret > 0.5 else "—", s["num"]),
            ])

        if registry is not None:
            # Mode registre : ordre / libellés / visibilité pilotés par le
            # registre. On saute les postes masqués (visible:false) et on
            # mélange fixes + perso dans l'ordre exact du registre.
            for entry in registry:
                if not isinstance(entry, dict):
                    continue
                key = str(entry.get("key") or "")
                if not key:
                    continue
                if entry.get("visible") is False:
                    continue
                if key in _fixed_label_by_key:
                    label = str(entry.get("label_fr")
                                or _fixed_label_by_key[key])
                    _append_fixed(key, label)
                elif key in custom_by_id:
                    c = custom_by_id[key]
                    label = str(entry.get("label_fr")
                                or c.get("label_fr") or "Frais personnalisé")
                    _append_custom(c, label)
                # Clé inconnue (ni fixe ni perso) → ignorée.
        else:
            # Fallback historique : postes fixes dans l'ordre `poste_defs`,
            # puis postes personnalisés.
            for key, label in poste_defs:
                _append_fixed(key, label)
            for c in (fd.get("frais_custom") or []):
                if not isinstance(c, dict):
                    continue
                clabel = str(c.get("label_fr") or "Frais personnalisé")
                _append_custom(c, clabel)

        # Total des frais de démarrage (cash sorti sur l'ensemble des
        # postes) + total de la portion financée par le prêteur B.
        data_rows.append([
            Paragraph("Total frais de démarrage", s["small"]),
            Paragraph(f"<b>{_money(fd_total)}</b>", s["num_b"]),
            Paragraph(
                f"<b>{_money(total_cash_finances)}</b>"
                if total_cash_finances > 0.5 else "—", s["num_b"]),
        ])
        data_rows.append([
            Paragraph("dont financé par prêteur B", s["small"]),
            Paragraph("—", s["num"]),
            Paragraph(
                f"<b>{_money(total_cash_finances)}</b>"
                if total_cash_finances > 0.5 else "—", s["num_b"]),
        ])
        # Assise nette : % du prix DÉCLARÉ − cashback reçu au notaire −
        # balance de vente (financée par le vendeur) — reflet de la
        # ligne de base de la fiche.
        _cb = float((results.get("cashback") or {}).get("montant") or 0)
        _bv = float((results.get("balance_vente") or {}).get("montant") or 0)
        _assise_lbl = "MDF (% × prix" + (" déclaré" if _cb > 0 else " d'achat") + ")"
        if _cb > 0:
            _assise_lbl += f" − cashback {_money(_cb)}"
        if _bv > 0:
            _assise_lbl += f" − balance de vente {_money(_bv)}"
        data_rows.append([
            Paragraph(_assise_lbl, s["small"]),
            Paragraph(_money(max(0.0, mdf_pct_amt - _cb - _bv)), s["num"]),
            Paragraph("—", s["num"]),
        ])
        data_rows.append([
            Paragraph("MDF prêteur B totale", s["small"]),
            Paragraph(f"<b>{_money(mdf_total)}</b>", s["num_b"]),
            Paragraph("—", s["num"]),
        ])

        t = Table(
            data_rows, colWidths=[78 * mm, "*", "*"], repeatRows=1)
        t.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                # En-tête ambre pâle.
                ("BACKGROUND", (0, 0), (-1, 0),
                 colors.HexColor(_C_AMBER_SOFT)),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75,
                 colors.HexColor(_C_AMBER_LINE)),
                # Filets entre les postes (jusqu'au dernier poste avant
                # les 4 lignes de total).
                ("LINEBELOW", (0, 1), (-1, -5), 0.25,
                 colors.HexColor(_C_LINE)),
                ("ROWBACKGROUNDS", (0, 1), (-1, -5),
                 [colors.white, colors.HexColor("#fafafa")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                # Total frais de démarrage (4e avant-dernière) ambre pâle.
                ("BACKGROUND", (0, -4), (-1, -4),
                 colors.HexColor(_C_AMBER_SOFT)),
                ("LINEABOVE", (0, -4), (-1, -4), 0.75,
                 colors.HexColor(_C_AMBER_LINE)),
                # « dont financé par prêteur B » (3e avant-dernière) vert.
                ("BACKGROUND", (0, -3), (-1, -3),
                 colors.HexColor(_C_GREEN_SOFT)),
                # MDF prêteur B totale (dernière) en fond vert pâle.
                ("BACKGROUND", (0, -1), (-1, -1),
                 colors.HexColor(_C_GREEN_SOFT)),
                ("LINEABOVE", (0, -1), (-1, -1), 0.75,
                 colors.HexColor(_C_GREEN_LINE)),
            ])
        )
        story.append(t)
    elif not results:
        story.append(Paragraph(
            "L'analyse financière n'a pas encore été lancée — "
            "les frais de démarrage seront calculés au prochain "
            "« Lancer l'analyse ».",
            s["small_muted"],
        ))

    # ── Scénarios de financement (tableau comparatif) ───────────
    # Phase 2 : une fiche en stratégie « achat direct » remplace le
    # tableau classique par la section dédiée (reflet de l'écran).
    _direct = (results or {}).get("traditionnel")
    _mode_direct = bool(
        results
        and _direct
        and getattr(rec, "strategie_acquisition", None)
        in (
            "traditionnel", "conventionnel", "schl_std",
            "aph_50", "aph_100",
        )
    )
    if _mode_direct:
        story.append(Paragraph(
            "INSTITUTION TRADITIONNELLE — ACHAT, DÉTENTION ET "
            "REFINANCEMENT", s["section"]
        ))
        story.extend(_traditionnel_section(rl, _direct, s=s))
        story.append(Spacer(1, 3))
        story.append(Paragraph(
            "Achat financé sur les loyers actuels (4 programmes "
            "comparés, le retenu pilote la mise de fonds) ; détention "
            "avec croissance organique ; refinancement comparé à "
            "l'horizon choisi.", s["small_muted"]))
        _un = results.get("unites")
        if _un:
            story.append(Paragraph(
                f"Optimisation par unité : {_un.get('optimisees')} unité(s) "
                f"optimisée(s) sur {_un.get('total')} — les autres "
                "croissent depuis leur loyer actuel.", s["small_muted"]))
    elif results:
        story.append(Paragraph(
            "SCÉNARIOS DE FINANCEMENT", s["section"]
        ))
        winner_key = _best_refi_key(results)
        story.append(_scenarios_table(
            rl, results, s=s, winner_key=winner_key,
            # PDF = reflet de la fiche : en stratégie prêteur B (fiches
            # du chantier staging), la colonne « Achat conventionnel »
            # est masquée comme à l'écran.
            masquer_achat=(
                getattr(rec, "strategie_acquisition", None) == "preteur_b"
            ),
        ))
        story.append(Spacer(1, 3))
        story.append(Paragraph(
            "La colonne surlignée en vert est le scénario gagnant "
            "(meilleure équité au refinancement).", s["small_muted"]))
        _un = results.get("unites")
        if _un:
            story.append(Paragraph(
                f"Optimisation par unité : {_un.get('optimisees')} unité(s) "
                f"optimisée(s) sur {_un.get('total')} — les unités non "
                "optimisées gardent leur loyer actuel au refinancement.",
                s["small_muted"]))
    else:
        story.append(Paragraph(
            "Aucun scénario calculé — lance l'analyse financière "
            "depuis la fiche pour générer les scénarios.",
            s["small_muted"],
        ))

    # ── Meilleur scénario refi ──────────────────────────────────
    # (Sans objet en achat direct : le verdict de l'an H est déjà dans
    # la section dédiée — reflet de la fiche.)
    if not _mode_direct:
        story.append(Paragraph(
            "MEILLEUR SCÉNARIO REFI", s["section"]
        ))
    if results and not _mode_direct:
        best = results.get("best_refi") or {}
        program = best.get("program") or rec.best_refi_program
        amount = best.get("amount") or rec.best_refi_amount
        rci, pvi = _compute_rci_pvi(rec, results)
        best_rows: List[tuple] = [
            ("Programme retenu", f"<b>{_str(program)}</b>"),
            ("Équité dégagée", _money(amount)),
            ("RCI (% capital récupéré)",
             _pct(rci, decimals=1) if rci is not None else "—"),
            ("PVI (prise de valeur de l'immeuble)",
             _money(pvi) if pvi is not None else "—"),
        ]
        win = _best_refi_scenario(results)
        if win:
            best_rows.append(
                ("Justification — RCD",
                 f"{float(win.get('rcd') or 0):.2f}"))
            best_rows.append(
                ("Justification — LTV",
                 f"{float(win.get('ltv') or 0) * 100:.0f} %"))
        t = _table_two_col(rl, best_rows, s=s)
        if t is not None:
            story.append(t)
    elif not _mode_direct:
        story.append(Paragraph(
            "Pas encore de meilleur scénario — lance l'analyse.",
            s["small_muted"],
        ))

    # ── Rendement de l'investisseur (TRI) ───────────────────────
    for fl in _tri_section(rl, rec, results, s=s):
        story.append(fl)

    # ── Validation ──────────────────────────────────────────────
    story.append(Paragraph("VALIDATION", s["section"]))
    warnings = rec.validation_warnings or []
    if not warnings:
        story.append(Paragraph(
            "Aucune anomalie détectée par le validateur post-extraction.",
            s["info_ok"],
        ))
    else:
        rows = [[
            Paragraph("Sévérité", s["th_left"]),
            Paragraph("Champ", s["th_left"]),
            Paragraph("Message", s["th_left"]),
        ]]
        for w in warnings:
            sev = _format_warning_severity(w.get("severity"))
            rows.append([
                Paragraph(sev, s["small"]),
                Paragraph(_str(w.get("field")), s["small"]),
                Paragraph(_str(w.get("message")), s["small"]),
            ])
        t = Table(rows, colWidths=[28 * mm, 38 * mm, "*"])
        t.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0),
                 colors.HexColor(_C_AMBER_SOFT)),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75,
                 colors.HexColor(_C_AMBER_LINE)),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#fafafa")]),
                ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor(_C_LINE)),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ])
        )
        story.append(t)

    # ── Sources & attachments ──────────────────────────────────
    story.append(Paragraph(
        "SOURCES &amp; ATTACHMENTS", s["section"]
    ))
    if rec.source_urls and rec.source_urls.strip():
        story.append(Paragraph("<b>URLs sources</b>", s["subsection"]))
        for line in rec.source_urls.splitlines():
            line = line.strip()
            if not line:
                continue
            story.append(Paragraph(line, s["small"]))
    else:
        story.append(Paragraph(
            "Aucune URL source.", s["small_muted"]
        ))

    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "<b>Fichiers joints</b>", s["subsection"]
    ))
    if attachments:
        att_rows = [[
            Paragraph("Nom", s["th_left"]),
            Paragraph("Type MIME", s["th_left"]),
            Paragraph("Taille", s["th_left"]),
        ]]
        for att in attachments:
            size_kb = (att.size_bytes or 0) / 1024.0
            size_str = (
                f"{size_kb:.1f} KB" if size_kb < 1024
                else f"{size_kb / 1024:.2f} MB"
            )
            att_rows.append([
                Paragraph(_str(att.filename), s["small"]),
                Paragraph(_str(att.content_type), s["small"]),
                Paragraph(size_str, s["num"]),
            ])
        t = Table(att_rows, colWidths=["55%", "30%", "15%"])
        t.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0),
                 colors.HexColor(_C_AMBER_SOFT)),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75,
                 colors.HexColor(_C_AMBER_LINE)),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#fafafa")]),
                ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor(_C_LINE)),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ])
        )
        story.append(t)
    else:
        story.append(Paragraph(
            "Aucun fichier joint.", s["small_muted"]
        ))

    # ── Build avec en-tête + pied de page sur chaque page ──────
    def _on_page(canvas, doc):
        _draw_page_furniture(
            canvas, doc,
            address_label=address_label, date_label=date_label,
        )

    try:
        doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    except Exception as exc:
        log.exception(
            "Rendu PDF fiche analyse %s échoué : %s", rec.id, exc
        )
        raise ValueError(
            f"Rendu du PDF de la fiche d'analyse échoué : "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return buf.getvalue()


def _slugify_address(addr: Optional[str], fallback_id: int) -> str:
    """Convertit l'adresse en slug ASCII pour le nom de fichier.
    Ex. « 1234 rue Sainte-Catherine, Montréal » → « 1234_rue_Sainte_Catherine_Montreal ».
    """
    if not addr or not addr.strip():
        return str(fallback_id)
    decomposed = unicodedata.normalize("NFKD", addr.strip())
    ascii_only = "".join(
        ch for ch in decomposed if not unicodedata.combining(ch)
    )
    underscored = re.sub(r"[\s\-,'’]+", "_", ascii_only)
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", underscored)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or str(fallback_id)


def lead_analysis_pdf_filename(rec: LeadAnalysis) -> str:
    """Nom de fichier canonique : `Fiche_Analyse_{slug}_{id}.pdf`."""
    slug = _slugify_address(rec.address, rec.id)
    return f"Fiche_Analyse_{slug}_{rec.id}.pdf"


async def generate_lead_analysis_pdf(
    db: AsyncSession, analysis_id: int
) -> bytes:
    """Génère à la volée le PDF complet de l'analyse — pas de stockage
    en BDD (l'export reflète toujours l'état courant)."""
    rec, atts = await _load(db, analysis_id)
    if rec is None:
        raise ValueError(f"LeadAnalysis {analysis_id} introuvable")
    return _render_bytes(rec, atts)
