"use client";

import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import type { ComponentType, ReactNode } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  Ban,
  Banknote,
  Building2,
  Calculator,
  CheckCircle2,
  ClipboardList,
  Coins,
  Download,
  FileText,
  Flame,
  Gauge,
  Info,
  ListChecks,
  Loader2,
  Percent,
  PiggyBank,
  Sparkles,
  TrendingUp,
  Wallet,
  X
} from "lucide-react";

import { authedFetch } from "@/lib/auth";
import { calculateurStrategiesActif } from "@/lib/feature-flags";
import { useConfirm } from "@/components/confirm-dialog";
import { PillPicker } from "@/components/task-pills";

/**
 * Modal de fiche d'analyse détaillée d'un lead. Extrait de la page
 * `prospection/analyses-leads/page.tsx` (où il était défini in-place
 * sur ~550 lignes + ~2300 lignes de helpers) pour devenir réutilisable
 * depuis la page d'un Deal Pipeline.
 *
 * Le composant est entièrement autonome :
 *   - charge la fiche via `GET /lead-analyses/{id}` au mount,
 *   - cleanup propre via flag `cancelled` à l'unmount,
 *   - notifie le parent à chaque sauvegarde via `onAfterUpdate`.
 *
 * Props :
 *   - `analysisId`      : id de la fiche d'analyse à charger,
 *   - `open`            : controlled — ne render que si true,
 *   - `onClose`         : fermeture (click overlay, X, Escape implicite),
 *   - `onAfterUpdate?`  : appelé après chaque patch réussi (refresh
 *                        kanban / résumé Deal),
 *   - `onBackToDeal?`   : si fourni, affiche un bouton « Retour au
 *                        deal » dans le header (cas `?fromDeal` sur la
 *                        page kanban Analyses).
 */

// ─── Types ───────────────────────────────────────────────────────

type LeadStatus = "a_analyser" | "decision_en_attente" | "interessant" | "abandonne";

/** Phase A3 — entrée individuelle d'anomalie post-extraction. */
export type ValidationWarning = {
  field: string;
  severity: "error" | "warning" | "info";
  message: string;
  source_local?: number | string | null;
  source_gemini?: number | string | null;
  source_claude?: number | string | null;
};

type LeadDetail = {
  id: number;
  status: LeadStatus;
  position: number;
  address: string | null;
  city: string | null;
  postal_code: string | null;
  province: string | null;
  asking_price: number | null;
  nb_logements: number | null;
  annee_construction: number | null;
  best_refi_amount: number | null;
  best_refi_program: string | null;
  mdf_preteur_b: number | null;
  type_batiment: string | null;
  converted_to_lead_id: number | null;
  converted_to_deal_id: number | null;
  model_used: string | null;
  validation_severity: "error" | "warning" | "info" | null;
  validation_count: number;
  created_at: string;
  attachments_count: number;
  typology_json: string | null;
  revenus_bruts: number | null;
  taxes_municipales: number | null;
  taxes_scolaires: number | null;
  assurances: number | null;
  energie: number | null;
  depenses_autres: number | null;
  superficie_terrain: number | null;
  superficie_batiment: number | null;
  evaluation_municipale: number | null;
  description: string | null;
  courtier_nom: string | null;
  courtier_contact: string | null;
  nb_stationnements: number | null;
  source_urls: string | null;
  source_text: string | null;
  notes: string | null;
  // Inputs manuels analyse financière
  loyers_projetes_json: string | null;
  loyers_max_abordabilite_json: string | null;
  travaux_estimes: number | null;
  nb_logements_ajoutes: number | null;
  nb_thermopompes_ajoutees: number | null;
  ajout_wifi: boolean | null;
  reduction_energie_pct: number | null;
  taux_interet_refi_pct: number | null;
  tga_pct: number | null;
  taux_interet_achat_pct: number | null;
  duree_projet_annees: number | null;
  frais_developpement: number | null;
  frais_negociations: number | null;
  mdf_preteur_b_pct: number | null;
  taux_interet_preteur_b_projet_pct: number | null;
  frais_demarrage_overrides_json: string | null;
  frais_demarrage_financables_json: string | null;
  strategie_acquisition: string | null;
  programme_achat?: string | null;
  refi_retenu?: string | null;
  balance_vente_montant: number | null;
  balance_vente_taux_pct: number | null;
  /** Cashback reçu au notaire (prix déclaré = prix + cashback). */
  cashback_montant?: number | null;
  projection_horizon_annees: number | null;
  /** FRACTIONS (0.03 = 3 %) — partagées avec le TRI. */
  tri_croissance_loyers?: number | null;
  tri_croissance_depenses?: number | null;
  /** Phase 3 — unités [{typo, loyer_actuel, loyer_cible, optimiser}]. */
  unites_json?: string | null;
  analysis_results_json: string | null;
  validation_warnings: ValidationWarning[] | null;
  attachments: Array<{
    id: number;
    filename: string;
    content_type: string;
    size_bytes: number;
  }>;
};

const COLUMNS: Array<{
  key: LeadStatus;
  label: string;
  dot: string;
  desc: string;
}> = [
  {
    key: "a_analyser",
    label: "À analyser",
    dot: "bg-violet-400",
    desc: "Fraîchement capturés"
  },
  {
    key: "decision_en_attente",
    label: "Décision en attente",
    dot: "bg-amber-400",
    desc: "Analyse complétée, à classer"
  },
  {
    key: "interessant",
    label: "Intéressant",
    dot: "bg-emerald-400",
    desc: "À pousser plus loin"
  },
  {
    key: "abandonne",
    label: "Abandonné / Rejeté",
    dot: "bg-rose-400",
    desc: "Hors critères"
  }
];

const TYPOLOGY_KEYS = ["1.5", "2.5", "3.5", "4.5", "5.5", "6.5", "7.5", "8.5"];

// ─── Onglets internes de la fiche ────────────────────────────────

type TabKey =
  | "infos"
  | "analyse"
  | "achat"
  | "resultats"
  | "details"
  | "tri";

// « Résultats » devient « Refinancement » (retour Phil 2026-09-02) ;
// en stratégie institution traditionnelle, un onglet « Achat »
// s'ajoute AVANT.
const TABS: Array<{
  key: TabKey;
  label: string;
  icon: ComponentType<{ className?: string }>;
}> = [
  { key: "infos", label: "Infos", icon: ClipboardList },
  { key: "analyse", label: "Analyse", icon: Calculator },
  { key: "achat", label: "Achat", icon: Banknote },
  { key: "resultats", label: "Refinancement", icon: TrendingUp },
  { key: "details", label: "Détails des calculs", icon: ListChecks },
  { key: "tri", label: "TRI", icon: Percent }
];

/**
 * Formatage monétaire unique de la fiche (style « 12 345 $ »). Source de
 * vérité unifiée : les anciens helpers `_formatMoneyExcel` et
 * `_fmtMoneyDetail` (logique identique) délèguent désormais ici.
 */
function fmtMoney(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const rounded = Math.round(n);
  const sign = rounded < 0 ? "-" : "";
  const abs = Math.abs(rounded).toString();
  const withSep = abs.replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  return `${sign}${withSep} $`;
}

// ─── Briques UI réutilisables (look 2026) ────────────────────────

type SectionTone = "neutral" | "accent" | "emerald" | "amber";

const SECTION_TONE: Record<
  SectionTone,
  { tile: string; border: string }
> = {
  neutral: {
    tile: "bg-white/[0.06] text-white/70",
    border: "border-brand-800 bg-brand-900"
  },
  accent: {
    tile: "bg-accent-500/15 text-accent-500",
    border: "border-accent-500/30 bg-accent-500/[0.06]"
  },
  emerald: {
    tile: "bg-emerald-500/15 text-emerald-400",
    border: "border-emerald-500/30 bg-emerald-500/[0.06]"
  },
  amber: {
    tile: "bg-amber-500/15 text-amber-400",
    border: "border-amber-500/30 bg-amber-500/[0.06]"
  }
};

/**
 * Carte de section « standard 2026 » : conteneur arrondi + en-tête à
 * tuile-icône colorée, titre `text-base font-bold`, sous-titre discret.
 * Imite `prospection/parametres/page.tsx`.
 */
function SectionCard({
  icon: Icon,
  title,
  subtitle,
  tone = "neutral",
  action,
  children,
  className = ""
}: {
  icon: ComponentType<{ className?: string }>;
  title: string;
  subtitle?: ReactNode;
  tone?: SectionTone;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const t = SECTION_TONE[tone];
  return (
    <section
      className={`rounded-2xl border p-5 ${t.border} ${className}`}
    >
      <header className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <span
            className={`flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl ${t.tile}`}
          >
            <Icon className="h-5 w-5" />
          </span>
          <div className="min-w-0">
            <h3 className="text-base font-bold text-white">{title}</h3>
            {subtitle ? (
              <p className="mt-0.5 text-xs text-white/60">{subtitle}</p>
            ) : null}
          </div>
        </div>
        {action ? <div className="flex-shrink-0">{action}</div> : null}
      </header>
      <div className="mt-4">{children}</div>
    </section>
  );
}

/**
 * Sous-carte thématique pour regrouper des inputs (ex. « Financement »,
 * « Frais & projet »). Micro-titre lisible + grille de champs.
 */
function SubCard({
  icon: Icon,
  title,
  children,
  cols = 3
}: {
  icon?: ComponentType<{ className?: string }>;
  title: string;
  children: ReactNode;
  cols?: 2 | 3 | 4;
}) {
  const gridCls =
    cols === 4
      ? "sm:grid-cols-4"
      : cols === 2
      ? "sm:grid-cols-2"
      : "sm:grid-cols-3";
  return (
    <div className="rounded-xl border border-brand-800 bg-brand-950/40 p-3.5">
      <div className="flex items-center gap-2">
        {Icon ? <Icon className="h-3.5 w-3.5 text-accent-500" /> : null}
        <p className="text-xs font-semibold text-white/80">{title}</p>
      </div>
      <div className={`mt-3 grid gap-3 ${gridCls}`}>{children}</div>
    </div>
  );
}

/**
 * Tuile de statistique pour la bande « hero metrics ». Signal couleur
 * emerald (bon) / rose (négatif) / neutre. Pattern compact inspiré de
 * `lead-analysis-summary.tsx`.
 */
function StatTile({
  icon: Icon,
  label,
  value,
  hint,
  tone = "neutral"
}: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: string;
  hint?: string;
  tone?: "neutral" | "emerald" | "rose" | "amber" | "accent";
}) {
  const toneCls =
    tone === "emerald"
      ? "text-emerald-300"
      : tone === "rose"
      ? "text-rose-300"
      : tone === "amber"
      ? "text-amber-200"
      : tone === "accent"
      ? "text-accent-500"
      : "text-white";
  const iconCls =
    tone === "emerald"
      ? "bg-emerald-500/15 text-emerald-400"
      : tone === "rose"
      ? "bg-rose-500/15 text-rose-400"
      : tone === "amber"
      ? "bg-amber-500/15 text-amber-400"
      : tone === "accent"
      ? "bg-accent-500/15 text-accent-500"
      : "bg-white/[0.06] text-white/60";
  return (
    <div className="rounded-xl border border-brand-800 bg-brand-900 p-3">
      <div className="flex items-center gap-2">
        <span
          className={`flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-lg ${iconCls}`}
        >
          <Icon className="h-3.5 w-3.5" />
        </span>
        <p className="truncate text-[10px] uppercase tracking-wider text-white/50">
          {label}
        </p>
      </div>
      <p
        className={`mt-1.5 truncate font-mono text-base font-bold tabular-nums ${toneCls}`}
        title={value}
      >
        {value}
      </p>
      {hint ? (
        <p className="mt-0.5 truncate text-[10px] text-white/40" title={hint}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}

/**
 * Hook de feedback de sauvegarde réutilisable. Affiche brièvement
 * « Enregistré ✓ » après chaque sauvegarde au blur (la sauvegarde était
 * jusqu'ici silencieuse). `markSaved()` à appeler après un patch réussi ;
 * `markError()` en cas d'échec.
 */
function useSaveFeedback() {
  const [state, setState] = useState<"idle" | "saving" | "saved" | "error">(
    "idle"
  );
  useEffect(() => {
    if (state !== "saved") return;
    const t = setTimeout(() => setState("idle"), 2200);
    return () => clearTimeout(t);
  }, [state]);
  return {
    state,
    markSaving: () => setState("saving"),
    markSaved: () => setState("saved"),
    markError: () => setState("error"),
    reset: () => setState("idle")
  };
}

/** Petit badge inline « Enregistré ✓ » piloté par `useSaveFeedback`. */
function SaveIndicator({
  state
}: {
  state: "idle" | "saving" | "saved" | "error";
}) {
  if (state === "idle") return null;
  if (state === "saving") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[10px] font-medium text-white/60">
        <Loader2 className="h-3 w-3 animate-spin" />
        Enregistrement…
      </span>
    );
  }
  if (state === "error") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-rose-500/40 bg-rose-500/10 px-2 py-0.5 text-[10px] font-medium text-rose-300">
        <AlertTriangle className="h-3 w-3" />
        Échec de l&apos;enregistrement
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium text-emerald-300">
      <CheckCircle2 className="h-3 w-3" />
      Enregistré
    </span>
  );
}

/** Encart vide pour un onglet sans données (Résultats / Détails). */
function EmptyTabHint({
  icon: Icon,
  message
}: {
  icon: ComponentType<{ className?: string }>;
  message: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-brand-800 bg-brand-900/40 px-6 py-12 text-center">
      <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-white/[0.04] text-white/40">
        <Icon className="h-6 w-6" />
      </span>
      <p className="max-w-sm text-sm text-white/50">{message}</p>
    </div>
  );
}

// ─── Composant principal ──────────────────────────────────────────

export function LeadAnalysisDetailModal({
  analysisId,
  open,
  onClose,
  onAfterUpdate,
  onBackToDeal
}: {
  analysisId: number;
  open: boolean;
  onClose: () => void;
  onAfterUpdate?: () => void;
  /** Si défini, affiche un bouton « Retour au deal » dans le header
   *  du modal — utilisé par la page kanban Analyses quand le modal est
   *  ouvert via ?openId={id}&fromDeal={dealId}. */
  onBackToDeal?: () => void;
}) {
  const [data, setData] = useState<LeadDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<TabKey>("infos");
  const save = useSaveFeedback();

  // Stratégie « institution traditionnelle » (alias inclus) → onglet
  // « Achat » visible ; s'il est actif quand la stratégie change, on
  // retombe sur Refinancement.
  const modeTradTop =
    (data?.strategie_acquisition ?? "preteur_b") !== "preteur_b";
  const tabEffectif: TabKey =
    tab === "achat" && !modeTradTop ? "resultats" : tab;

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const ctrl = new AbortController();
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const r = await authedFetch(`/api/v1/lead-analyses/${analysisId}`, {
          signal: ctrl.signal
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = (await r.json()) as LeadDetail;
        if (!cancelled) setData(j);
      } catch (e) {
        if (!cancelled && (e as Error).name !== "AbortError") {
          setError((e as Error).message);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [analysisId, open]);

  async function patchField(field: string, value: unknown) {
    if (!data) return;
    setData({ ...data, [field]: value } as LeadDetail);
    save.markSaving();
    try {
      const r = await authedFetch(`/api/v1/lead-analyses/${analysisId}`, {
        method: "PATCH",
        body: JSON.stringify({ [field]: value })
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      // Le backend peut avoir RECALCULÉ les résultats (si le champ
      // modifié est un intrant du calcul — ex. décocher un poste
      // finançable prêteur B). On fusionne les champs DÉRIVÉS de la
      // réponse pour rafraîchir l'affichage, sans écraser les intrants
      // en cours d'édition.
      try {
        const updated = (await r.json()) as Record<string, unknown>;
        if (updated && typeof updated === "object") {
          setData((prev) => {
            if (!prev) return prev;
            const merged = { ...prev } as Record<string, unknown>;
            for (const f of [
              "analysis_results_json",
              "mdf_preteur_b",
              "best_refi_amount",
              "best_refi_program",
              "status",
              "validation_warnings"
            ]) {
              if (f in updated) merged[f] = updated[f];
            }
            return merged as LeadDetail;
          });
        }
      } catch {
        /* réponse non-JSON : on conserve l'état optimistic */
      }
      save.markSaved();
      onAfterUpdate?.();
    } catch {
      /* local state retained — surface a discreet error */
      save.markError();
    }
  }

  // ── Estimation IA des dépenses manquantes ─────────────────────
  const [estimatingExpenses, setEstimatingExpenses] = useState(false);
  const [estimateMsg, setEstimateMsg] = useState<{
    text: string;
    kind: "ok" | "warn" | "err";
  } | null>(null);
  async function estimateExpenses() {
    if (!data) return;
    setEstimatingExpenses(true);
    setEstimateMsg(null);
    try {
      const r = await authedFetch(
        `/api/v1/lead-analyses/${analysisId}/estimate-expenses`,
        { method: "POST" }
      );
      if (!r.ok) {
        setEstimateMsg({
          text: `Estimation échouée (HTTP ${r.status})`,
          kind: "err"
        });
        return;
      }
      const out = (await r.json()) as {
        taxes_municipales: number | null;
        taxes_scolaires: number | null;
        assurances: number | null;
        source?: string;
        note?: string;
      };
      const patch: Record<string, number> = {};
      if (data.taxes_municipales == null && out.taxes_municipales != null) {
        patch.taxes_municipales = out.taxes_municipales;
      }
      if (data.taxes_scolaires == null && out.taxes_scolaires != null) {
        patch.taxes_scolaires = out.taxes_scolaires;
      }
      if (data.assurances == null && out.assurances != null) {
        patch.assurances = out.assurances;
      }
      if (Object.keys(patch).length > 0) {
        setData({ ...data, ...patch } as LeadDetail);
        await authedFetch(`/api/v1/lead-analyses/${analysisId}`, {
          method: "PATCH",
          body: JSON.stringify(patch)
        });
        onAfterUpdate?.();
        const labels = Object.keys(patch)
          .map((k) =>
            k === "taxes_municipales"
              ? "taxes muni"
              : k === "taxes_scolaires"
              ? "taxes scol"
              : "assurances"
          )
          .join(", ");
        setEstimateMsg({
          text: `${labels} estimé${
            Object.keys(patch).length > 1 ? "s" : ""
          } via ${out.source || "IA"}.`,
          kind: "ok"
        });
      } else {
        const reason =
          out.note ||
          "L'IA n'a pas pu estimer — vérifie que le prix demandé et le nombre de logements sont renseignés.";
        setEstimateMsg({ text: reason, kind: "warn" });
      }
    } catch (e) {
      setEstimateMsg({
        text: `Estimation échouée : ${(e as Error).message}`,
        kind: "err"
      });
    } finally {
      setEstimatingExpenses(false);
    }
  }

  const typology = useMemo(() => {
    if (!data?.typology_json) return null;
    try {
      return JSON.parse(data.typology_json) as Record<string, number>;
    } catch {
      return null;
    }
  }, [data?.typology_json]);

  // ── Export PDF de la fiche d'analyse ──────────────────────────
  // Pattern `openAuthedPdf` repris de `nda-section.tsx` (PR #526) :
  // le browser ne joint pas le header `Authorization` sur une nav
  // top-level, donc on fetch en blob puis on ouvre une URL blob.
  const [pdfBusy, setPdfBusy] = useState(false);
  const [pdfToast, setPdfToast] = useState<{
    text: string;
    kind: "ok" | "err";
  } | null>(null);

  useEffect(() => {
    if (!pdfToast || pdfToast.kind === "err") return;
    const t = setTimeout(() => setPdfToast(null), 4000);
    return () => clearTimeout(t);
  }, [pdfToast]);

  async function downloadPdf() {
    if (pdfBusy) return;
    setPdfBusy(true);
    setPdfToast(null);
    try {
      const r = await authedFetch(
        `/api/v1/lead-analyses/${analysisId}/pdf`
      );
      if (!r.ok) {
        const detail = await r
          .json()
          .then((j: { detail?: string }) => j.detail || `HTTP ${r.status}`)
          .catch(() => `HTTP ${r.status}`);
        setPdfToast({
          text: `Génération PDF échouée : ${detail}`,
          kind: "err"
        });
        return;
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank");
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
      setPdfToast({ text: "PDF généré.", kind: "ok" });
    } catch (e) {
      setPdfToast({
        text: `Génération PDF échouée : ${(e as Error).message}`,
        kind: "err"
      });
    } finally {
      setPdfBusy(false);
    }
  }

  // ── Hero metrics : chiffres clés dérivés du dernier calcul ──────
  const hero = useMemo(() => {
    if (!data) return null;
    let results: AnalysisResults | null = null;
    try {
      results = data.analysis_results_json
        ? (JSON.parse(data.analysis_results_json) as AnalysisResults)
        : null;
    } catch {
      results = null;
    }
    // Best refi : on retient le scénario gagnant pour afficher son
    // cashflow / son équité dans la bande hero. On l'identifie par son
    // équité finale (= best_refi.amount, cf. « Montant (équité finale) »),
    // avec repli sur le libellé puis sur le meilleur refi disponible.
    // NB : purement indicatif — les chiffres faisant foi restent le
    // tableau de résultats, intact.
    const scen = results?.scenarios;
    const bestProgram = results?.best_refi.program ?? data.best_refi_program;
    const bestAmount = results?.best_refi.amount ?? null;
    let winner: ScenarioResult | null = null;
    if (scen) {
      const refis: Array<ScenarioResult | null> = [
        scen.refi_aph_100,
        scen.refi_aph_50,
        scen.refi_schl
      ];
      winner =
        (bestAmount != null
          ? refis.find(
              (s) =>
                s &&
                s.equite_a_la_fin != null &&
                Math.abs(s.equite_a_la_fin - bestAmount) < 1
            )
          : null) ||
        (bestProgram
          ? refis.find((s) => s && (s.label === bestProgram || s.name === bestProgram))
          : null) ||
        scen.refi_aph_100 ||
        scen.refi_aph_50 ||
        scen.refi_schl ||
        null;
    }
    // Institution traditionnelle : les tuiles reflètent l'onglet
    // Refinancement (prêt de la référence, cash à l'achat, cashflow,
    // argent dégagé) — plus les chiffres du mode prêteur B.
    const trad = results?.traditionnel ?? null;
    if (trad) {
      const refKey = trad.best_refi?.key;
      const ref = refKey ? trad.refi?.[refKey] ?? null : null;
      return {
        askingPrice: data.asking_price,
        bestRefiFinancement: ref?.financement ?? null,
        bestRefiProgram: trad.best_refi
          ? `${trad.best_refi.label} · refi an ${trad.horizon}`
          : null,
        mdf: trad.mdf_cash,
        cashflow: ref?.cashflow_annuel ?? null,
        equite: trad.best_refi?.argent_dispo ?? null,
        hasResults: true
      };
    }
    return {
      askingPrice: data.asking_price,
      // Montant de prêt accordé du scénario gagnant (champ `financement`,
      // = métrique « Prêt accordé »). C'est cette valeur — et non l'équité
      // finale — qu'affiche désormais la tuile « Best refi », pour ne plus
      // faire doublon avec la tuile « Équité à la fin ».
      bestRefiFinancement: winner?.financement ?? null,
      bestRefiProgram: bestProgram ?? null,
      mdf: results?.mdf_preteur_b ?? data.mdf_preteur_b,
      cashflow: winner?.cashflow_annuel ?? null,
      equite: winner?.equite_a_la_fin ?? null,
      hasResults: !!results
    };
    // Deps restreintes aux seuls champs de `data` réellement lus par ce
    // memo : `setData({...data, [field]:value})` change la référence de
    // `data` à chaque frappe optimiste, ce qui re-déclenchait le JSON.parse
    // inutilement. Ces 4 champs suffisent (mêmes valeurs de sortie).
  }, [
    data?.analysis_results_json,
    data?.best_refi_program,
    data?.mdf_preteur_b,
    data?.asking_price
  ]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 px-2 py-4 sm:items-center"
      onClick={onClose}
    >
      <div
        className="flex max-h-[calc(100vh-2rem)] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-brand-800 bg-brand-950"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex flex-shrink-0 items-start justify-between gap-3 border-b border-brand-800 px-5 py-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <p className="text-[10px] uppercase tracking-wider text-accent-500">
                Fiche d&apos;analyse
              </p>
              <SaveIndicator state={save.state} />
            </div>
            <h2 className="mt-0.5 truncate text-lg font-bold text-white">
              {data?.address || `Lead #${analysisId}`}
            </h2>
            {data ? <ExtractionBadgeInline modelUsed={data.model_used} /> : null}
          </div>
          <div className="flex flex-shrink-0 items-center gap-1">
            {onBackToDeal ? (
              <button
                type="button"
                onClick={onBackToDeal}
                className="inline-flex items-center gap-1 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-1 text-[11px] text-emerald-300 hover:bg-emerald-500/20"
                title="Revenir à la page du deal"
              >
                <ArrowLeft className="h-3 w-3" />
                Retour au deal
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => void downloadPdf()}
              disabled={pdfBusy || !data}
              title="Télécharger la fiche complète en PDF"
              className="inline-flex items-center gap-1 rounded-md border border-blue-500/30 bg-blue-500/10 px-2 py-1 text-[11px] font-medium text-blue-300 transition hover:bg-blue-500/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {pdfBusy ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Download className="h-3 w-3" />
              )}
              {pdfBusy ? "Génération…" : "PDF"}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md p-1 text-white/60 hover:bg-brand-900 hover:text-white"
              aria-label="Fermer"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </header>
        {pdfToast ? (
          <div
            className={`flex items-start gap-2 border-b border-brand-800 px-5 py-2 text-[11px] ${
              pdfToast.kind === "ok"
                ? "bg-emerald-500/10 text-emerald-300"
                : "bg-rose-500/10 text-rose-300"
            }`}
          >
            {pdfToast.kind === "ok" ? (
              <CheckCircle2 className="h-3 w-3 flex-shrink-0" />
            ) : (
              <AlertTriangle className="h-3 w-3 flex-shrink-0" />
            )}
            <p className="flex-1">{pdfToast.text}</p>
            {pdfToast.kind === "err" ? (
              <button
                type="button"
                onClick={() => setPdfToast(null)}
                className="ml-1 shrink-0 text-rose-300 hover:text-rose-100"
                aria-label="Fermer le message"
              >
                ✕
              </button>
            ) : null}
          </div>
        ) : null}

        {loading ? (
          <div className="flex-1 overflow-y-auto px-5 py-4">
            <p className="py-12 text-center text-sm text-white/40">
              <Loader2 className="mr-2 inline h-4 w-4 animate-spin" />
              Chargement…
            </p>
          </div>
        ) : error ? (
          <div className="flex-1 overflow-y-auto px-5 py-4">
            <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
              {error}
            </p>
          </div>
        ) : !data ? null : (
          <>
            {/* ── Zone fixe : Statut + hero metrics + onglets ─────── */}
            <div className="flex-shrink-0 border-b border-brand-800 bg-brand-950 px-5 pt-4">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-accent-500">
                  Statut
                </p>
                <PillPicker
                  options={COLUMNS.map((c) => ({
                    value: c.key,
                    label: c.label,
                    dot: c.dot,
                    cls: c.dot
                  }))}
                  value={data.status}
                  onChange={(v) => patchField("status", v)}
                  ariaLabel="Statut du lead"
                />
              </div>

              {/* Bande de hero metrics */}
              {hero ? (
                <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
                  <StatTile
                    icon={Banknote}
                    label="Prix demandé"
                    value={fmtMoney(hero.askingPrice)}
                    tone="accent"
                  />
                  <StatTile
                    icon={TrendingUp}
                    label="Best refi"
                    value={fmtMoney(hero.bestRefiFinancement)}
                    hint={hero.bestRefiProgram || undefined}
                    tone={
                      hero.bestRefiFinancement == null
                        ? "neutral"
                        : hero.bestRefiFinancement >= 0
                        ? "emerald"
                        : "rose"
                    }
                  />
                  <StatTile
                    icon={Wallet}
                    label={
                      modeTradTop ? "Mise de fonds (cash)" : "MDF prêteur B"
                    }
                    value={fmtMoney(hero.mdf)}
                    tone="amber"
                  />
                  <StatTile
                    icon={Coins}
                    label="Cashflow / an"
                    value={hero.cashflow != null ? fmtMoney(hero.cashflow) : "—"}
                    tone={
                      hero.cashflow == null
                        ? "neutral"
                        : hero.cashflow >= 0
                        ? "emerald"
                        : "rose"
                    }
                  />
                  <StatTile
                    icon={PiggyBank}
                    label={modeTradTop ? "Argent dégagé (refi)" : "Équité à la fin"}
                    value={hero.equite != null ? fmtMoney(hero.equite) : "—"}
                    tone={
                      hero.equite == null
                        ? "neutral"
                        : hero.equite >= 0
                        ? "emerald"
                        : "rose"
                    }
                  />
                </div>
              ) : null}

              {/* Barre d'onglets — « Achat » seulement en stratégie
                  institution traditionnelle. */}
              <div className="mt-3 flex gap-1 overflow-x-auto">
                {TABS.filter(
                  (t) => t.key !== "achat" || modeTradTop
                ).map((t) => {
                  const active = tabEffectif === t.key;
                  const Icon = t.icon;
                  return (
                    <button
                      key={t.key}
                      type="button"
                      onClick={() => setTab(t.key)}
                      className={`inline-flex flex-shrink-0 items-center gap-1.5 rounded-t-lg border-b-2 px-3 py-2 text-xs font-semibold transition ${
                        active
                          ? "border-accent-500 text-white"
                          : "border-transparent text-white/50 hover:text-white/80"
                      }`}
                    >
                      <Icon className="h-3.5 w-3.5" />
                      {t.label}
                    </button>
                  );
                })}
              </div>
            </div>

            {/* ── Zone scrollable : contenu de l'onglet actif ────── */}
            <div className="flex-1 overflow-y-auto px-5 py-4">
              {tabEffectif === "infos" ? (
                <div className="space-y-5">
              <SectionCard
                icon={Building2}
                title="Infos extraites"
                tone="neutral"
                subtitle="Champs pré-remplis par l'IA — clique pour corriger. Les champs vides sont à compléter manuellement."
              >
                <div className="grid gap-3 sm:grid-cols-2">
                  <FieldText
                    label="Adresse"
                    value={data.address}
                    onSave={(v) => patchField("address", v)}
                    required
                  />
                  <FieldText
                    label="Ville"
                    value={data.city}
                    onSave={(v) => patchField("city", v)}
                    required
                  />
                  <FieldText
                    label="Code postal"
                    value={data.postal_code}
                    onSave={(v) => patchField("postal_code", v)}
                  />
                  <FieldText
                    label="Type bâtiment"
                    value={data.type_batiment}
                    onSave={(v) => patchField("type_batiment", v)}
                  />
                  <FieldNumber
                    label="Prix demandé ($)"
                    name="asking_price"
                    value={data.asking_price}
                    onSave={(v) => patchField("asking_price", v)}
                    required
                    format="money"
                  />
                  <FieldNumber
                    label="Année construction"
                    value={data.annee_construction}
                    onSave={(v) => patchField("annee_construction", v)}
                  />
                  <FieldNumber
                    label="Nb logements"
                    name="nb_logements"
                    value={data.nb_logements}
                    onSave={(v) => patchField("nb_logements", v)}
                    required
                  />
                  <FieldNumber
                    label="Nb stationnements"
                    value={data.nb_stationnements}
                    onSave={(v) => patchField("nb_stationnements", v)}
                  />
                  <FieldNumber
                    label="Revenus bruts ($/an)"
                    name="revenus_bruts"
                    value={data.revenus_bruts}
                    onSave={(v) => patchField("revenus_bruts", v)}
                    required
                    format="money"
                  />
                  <FieldNumber
                    label="Évaluation municipale ($)"
                    value={data.evaluation_municipale}
                    onSave={(v) => patchField("evaluation_municipale", v)}
                    format="money"
                  />
                  <FieldNumber
                    label="Taxes municipales ($/an)"
                    name="taxes_municipales"
                    value={data.taxes_municipales}
                    onSave={(v) => patchField("taxes_municipales", v)}
                    required
                    onEstimate={() => void estimateExpenses()}
                    estimating={estimatingExpenses}
                    format="money"
                  />
                  <FieldNumber
                    label="Taxes scolaires ($/an)"
                    name="taxes_scolaires"
                    value={data.taxes_scolaires}
                    onSave={(v) => patchField("taxes_scolaires", v)}
                    required
                    onEstimate={() => void estimateExpenses()}
                    estimating={estimatingExpenses}
                    format="money"
                  />
                  <FieldNumber
                    label="Assurances ($/an)"
                    name="assurances"
                    value={data.assurances}
                    onSave={(v) => patchField("assurances", v)}
                    required
                    onEstimate={() => void estimateExpenses()}
                    estimating={estimatingExpenses}
                    format="money"
                  />
                  <FieldNumber
                    label="Énergie ($/an)"
                    name="energie"
                    value={data.energie}
                    onSave={(v) => patchField("energie", v)}
                    format="money"
                  />
                  <FieldNumber
                    label="Superficie terrain"
                    value={data.superficie_terrain}
                    onSave={(v) => patchField("superficie_terrain", v)}
                  />
                  <FieldNumber
                    label="Superficie bâtiment"
                    value={data.superficie_batiment}
                    onSave={(v) => patchField("superficie_batiment", v)}
                  />
                  <FieldText
                    label="Courtier (nom)"
                    value={data.courtier_nom}
                    onSave={(v) => patchField("courtier_nom", v)}
                  />
                  <FieldText
                    label="Courtier (contact)"
                    value={data.courtier_contact}
                    onSave={(v) => patchField("courtier_contact", v)}
                  />
                </div>

                {estimateMsg ? (
                  <p
                    className={`mt-2 rounded-lg border px-3 py-2 text-[11px] ${
                      estimateMsg.kind === "ok"
                        ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                        : estimateMsg.kind === "warn"
                        ? "border-amber-500/50 bg-amber-500/10 text-amber-200"
                        : "border-rose-500/40 bg-rose-500/10 text-rose-300"
                    }`}
                  >
                    {estimateMsg.text}
                  </p>
                ) : null}

                <div className="mt-4 rounded-xl border border-brand-800 bg-brand-950/40 p-3.5">
                  <p className="text-xs font-semibold text-white/80">
                    Typologie des logements
                  </p>
                  <p className="mt-0.5 text-[10px] text-white/40">
                    Quantité par typologie — modifiable si Claude n&apos;a
                    pas trouvé ou si tu veux corriger.
                  </p>
                  <TypologyEditor
                    value={typology || {}}
                    onSave={(j) =>
                      patchField("typology_json", JSON.stringify(j))
                    }
                  />
                </div>

                <div className="mt-4">
                  <label className="label !text-xs">Description</label>
                  <textarea
                    rows={3}
                    value={data.description || ""}
                    onChange={(e) =>
                      setData({ ...data, description: e.target.value })
                    }
                    onBlur={(e) => patchField("description", e.target.value)}
                    placeholder="Description / notes du courtier"
                    className="input text-xs"
                  />
                </div>
              </SectionCard>

              {/* Sources originales */}
              {data.source_urls || data.source_text || data.attachments?.length ? (
                <SectionCard
                  icon={FileText}
                  title="Sources originales"
                  tone="neutral"
                  action={
                    <ReExtractButtons
                      id={analysisId}
                      hasSources={
                        !!(
                          data.source_urls ||
                          data.source_text ||
                          (data.attachments && data.attachments.length > 0)
                        )
                      }
                      onSuccess={async () => {
                        const r = await authedFetch(
                          `/api/v1/lead-analyses/${analysisId}`
                        );
                        if (r.ok) setData((await r.json()) as LeadDetail);
                        onAfterUpdate?.();
                      }}
                    />
                  }
                >
                  {data.source_urls ? (
                    <div className="space-y-1">
                      {data.source_urls
                        .split("\n")
                        .filter((u) => u.trim())
                        .map((u, i) => (
                          <a
                            key={i}
                            href={u}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="block truncate text-xs text-violet-300 hover:underline"
                          >
                            🔗 {u}
                          </a>
                        ))}
                    </div>
                  ) : null}
                  {data.attachments?.length ? (
                    <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
                      {data.attachments.map((a) => (
                        <AttachmentThumb
                          key={a.id}
                          leadId={analysisId}
                          attachment={a}
                        />
                      ))}
                    </div>
                  ) : null}
                  {data.source_text ? (
                    <details className="mt-2">
                      <summary className="cursor-pointer text-[11px] text-white/50">
                        Texte original collé
                      </summary>
                      <pre className="mt-1 whitespace-pre-wrap rounded-md border border-brand-800 bg-brand-950 p-2 text-[11px] text-white/70">
                        {data.source_text}
                      </pre>
                    </details>
                  ) : null}
                </SectionCard>
              ) : null}

              {/* Phase A3 — Panneau "Validation de l'extraction" */}
              <ValidationPanel warnings={data.validation_warnings} />

              {/* Notes internes */}
              <SectionCard
                icon={FileText}
                title="Notes internes"
                tone="neutral"
                subtitle="Tes notes privées sur ce lead — visibles uniquement dans la fiche."
              >
                <textarea
                  rows={3}
                  value={data.notes || ""}
                  onChange={(e) =>
                    setData({ ...data, notes: e.target.value })
                  }
                  onBlur={(e) => patchField("notes", e.target.value)}
                  placeholder="Tes notes privées sur ce lead"
                  className="input text-xs"
                />
              </SectionCard>
                </div>
              ) : null}

              {tabEffectif === "analyse" ? (
                <div className="space-y-5">
                  {/* Section Analyse financière — inputs manuels + bouton */}
                  <ManualAnalysisSection
                    data={data}
                    onPatch={patchField}
                    onRefresh={async () => {
                      const r = await authedFetch(
                        `/api/v1/lead-analyses/${analysisId}`
                      );
                      if (r.ok) setData((await r.json()) as LeadDetail);
                      onAfterUpdate?.();
                    }}
                  />
                </div>
              ) : null}

              {tabEffectif === "achat" ? (
                <div className="space-y-5">
                  {(() => {
                    let trad: AnalysisResults["traditionnel"] = null;
                    try {
                      trad = data.analysis_results_json
                        ? (
                            JSON.parse(
                              data.analysis_results_json
                            ) as AnalysisResults
                          ).traditionnel
                        : null;
                    } catch {
                      trad = null;
                    }
                    return trad ? (
                      <TraditionnelAchatPanel
                        t={trad}
                        programmeChoisi={data.programme_achat ?? null}
                        onPatchField={patchField}
                      />
                    ) : (
                      <EmptyTabHint
                        icon={Banknote}
                        message="Lance l'analyse (onglet « Analyse ») pour voir le financement à l'achat des 4 programmes."
                      />
                    );
                  })()}
                </div>
              ) : null}

              {tabEffectif === "resultats" ? (
                <div className="space-y-5">
                  {data.analysis_results_json ? (
                    <AnalysisResultsTable
                      resultsJson={data.analysis_results_json}
                      overridesJson={data.frais_demarrage_overrides_json}
                      financablesJson={data.frais_demarrage_financables_json}
                      mdfPct={data.mdf_preteur_b_pct ?? 25}
                      prixAchat={data.asking_price ?? 0}
                      fraisDemarrageTotalDb={null}
                      mdfPreteurBDb={data.mdf_preteur_b ?? null}
                      onPatchField={patchField}
                      refiRetenu={data.refi_retenu ?? null}
                      onPatchOverrides={(j) =>
                        patchField("frais_demarrage_overrides_json", j)
                      }
                      onPatchFinancables={(j) =>
                        patchField("frais_demarrage_financables_json", j)
                      }
                    />
                  ) : (
                    <EmptyTabHint
                      icon={TrendingUp}
                      message="Aucun résultat pour le moment. Renseigne les inputs dans l'onglet « Analyse » puis lance le calcul."
                    />
                  )}
                </div>
              ) : null}

              {tabEffectif === "details" ? (
                <div className="space-y-5">
                  {data.analysis_results_json ? (
                    <CalculationDetailsSection
                      resultsJson={data.analysis_results_json}
                      overridesJson={data.frais_demarrage_overrides_json}
                      lead={data}
                    />
                  ) : (
                    <EmptyTabHint
                      icon={ListChecks}
                      message="Le détail granulaire des calculs apparaîtra ici une fois l'analyse lancée."
                    />
                  )}
                </div>
              ) : null}

              {tabEffectif === "tri" ? <LeadTriTab analysisId={analysisId} /> : null}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ─── Onglet TRI investisseur ──────────────────────────────────────
//
// Rendement de l'investisseur selon l'horizon de sortie (an 2 / 7 / 12),
// basé sur le scénario de refinancement retenu. 4 intrants manuels +
// 8 intrants repris de l'analyse (éditables). Endpoints :
//   GET  /lead-analyses/{id}/tri-inputs  → pré-remplissage
//   POST /lead-analyses/{id}/tri         → dict riche du moteur

/** Les 12 intrants envoyés au moteur (formes exactes du backend). */
type TriInputs = {
  /** Année du refinancement : horizons n, n+5, n+10. */
  annee_refi?: number;
  prix: number;
  rpv_achat: number;
  pret_constr: number;
  mdf: number;
  capital: number;
  pct: number;
  loyers2: number;
  dep2: number;
  valeur2: number;
  rpv_refi: number;
  cr_loyers: number;
  cr_dep: number;
};

type TriInputsResponse = {
  inputs: TriInputs;
  analysis_ready: boolean;
  auto_fields: string[];
  manual_fields: string[];
};

/** Détail d'un horizon de sortie (clés exactes du moteur). */
type TriHorizon = {
  loyers: number;
  depenses: number;
  rno: number;
  valeur_immeuble: number;
  pret_max_refi: number;
  argent_dispo: number;
  equite: number;
  retour_capital: number;
  surplus: number;
  cash_investisseur: number;
  valeur_parts: number;
};

/** Dict riche renvoyé par POST /tri (clés exactes du backend). */
type TriResult = {
  intrants: TriInputs;
  bases: {
    hypotheque: number;
    marge: number;
    rno2: number;
    multiplicateur: number;
    cap_rate: number;
  };
  /** Clés = années des horizons (n, n+5, n+10). */
  horizons: Record<string, TriHorizon>;
  sommaire: Record<string, number>;
  flux: Record<string, number[]>;
  tri: Record<string, number | null>;
  annee_refi?: number;
  horizons_list?: number[];
};

/** Les 3 horizons modélisés (ordre d'affichage) — dérivés du résultat :
 *  année du refi de l'analyse, +5 et +10 ans (retour Phil 2026-09-04). */
type TriHorizonDef = { key: string; tri: string; label: string };
function triHorizons(result: TriResult): TriHorizonDef[] {
  const list =
    result.horizons_list && result.horizons_list.length === 3
      ? result.horizons_list
      : [2, 7, 12];
  return list.map((h) => ({
    key: String(h),
    tri: `an${h}`,
    label: `an ${h}`
  }));
}

/** Champs « repris de l'analyse » : libellé + format d'affichage. */
const TRI_AUTO_FIELDS: Array<{
  key: keyof TriInputs;
  label: string;
  format: "money" | "percent" | "number";
}> = [
  {
    key: "annee_refi",
    label: "Année du refinancement (horizons n, n+5, n+10)",
    format: "number"
  },
  { key: "prix", label: "Prix d'achat", format: "money" },
  { key: "rpv_achat", label: "Ratio prêt-valeur (pré-construction)", format: "percent" },
  { key: "pret_constr", label: "Prêt construction", format: "money" },
  { key: "mdf", label: "Mise de fonds nécessaire", format: "money" },
  { key: "loyers2", label: "Loyers bruts stabilisés (année du refi)", format: "money" },
  { key: "dep2", label: "Dépenses d'opération (année du refi)", format: "money" },
  { key: "valeur2", label: "Valeur de l'immeuble stabilisée (année du refi)", format: "money" },
  { key: "rpv_refi", label: "Ratio prêt-valeur au refinancement", format: "percent" }
];

/** % affiché à 1 décimale (les intrants ratio sont stockés en fraction). */
function _fmtPctFraction(n: number | null | undefined, decimals = 1): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(decimals)} %`;
}

/** TRI renvoyé par le moteur (fraction) → « 12.3 % » ou « n/d ». */
function _fmtTri(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "n/d";
  return `${(n * 100).toFixed(1)} %`;
}

function LeadTriTab({ analysisId }: { analysisId: number }) {
  const [inputs, setInputs] = useState<TriInputs | null>(null);
  const [analysisReady, setAnalysisReady] = useState(true);
  const [loadingInputs, setLoadingInputs] = useState(true);
  const [inputsError, setInputsError] = useState<string | null>(null);

  const [result, setResult] = useState<TriResult | null>(null);
  const [computing, setComputing] = useState(false);
  const [computeError, setComputeError] = useState<string | null>(null);

  const [autoOpen, setAutoOpen] = useState(false);

  // Chargement des intrants pré-remplis.
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    (async () => {
      setLoadingInputs(true);
      setInputsError(null);
      try {
        const r = await authedFetch(
          `/api/v1/lead-analyses/${analysisId}/tri-inputs`,
          { signal: ctrl.signal }
        );
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const json = (await r.json()) as TriInputsResponse;
        if (cancelled) return;
        setInputs(json.inputs);
        setAnalysisReady(json.analysis_ready);
      } catch (e) {
        if (!cancelled && (e as Error).name !== "AbortError") {
          setInputsError("Impossible de charger les intrants du TRI.");
        }
      } finally {
        if (!cancelled) setLoadingInputs(false);
      }
    })();
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [analysisId]);

  function setField(key: keyof TriInputs, value: number | null) {
    setInputs((prev) => (prev ? { ...prev, [key]: value ?? 0 } : prev));
  }

  async function compute() {
    if (!inputs) return;
    setComputing(true);
    setComputeError(null);
    try {
      const r = await authedFetch(`/api/v1/lead-analyses/${analysisId}/tri`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(inputs)
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const json = (await r.json()) as TriResult;
      setResult(json);
    } catch {
      setComputeError("Le calcul du TRI a échoué. Réessaie.");
    } finally {
      setComputing(false);
    }
  }

  if (loadingInputs) {
    return (
      <div className="flex items-center justify-center gap-2 py-16 text-sm text-white/50">
        <Loader2 className="h-4 w-4 animate-spin" />
        Chargement des intrants…
      </div>
    );
  }

  if (inputsError || !inputs) {
    return (
      <EmptyTabHint
        icon={Percent}
        message={inputsError || "Intrants du TRI indisponibles."}
      />
    );
  }

  return (
    <div className="space-y-5">
      {/* En-tête de l'onglet */}
      <SectionCard
        icon={Percent}
        title="TRI investisseur"
        tone="emerald"
        subtitle="Rendement de l'investisseur selon l'horizon de sortie — année du refi de l'analyse, puis +5 et +10 ans, basé sur la référence de refinancement."
      >
        {!analysisReady ? (
          <div className="mb-4 flex items-start gap-2 rounded-xl border border-amber-500/30 bg-amber-500/[0.06] px-3 py-2 text-xs text-amber-200">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
            <span>
              L&apos;analyse financière n&apos;a pas encore tourné : les
              intrants repris sont à 0. Lance l&apos;analyse dans
              l&apos;onglet « Analyse » ou saisis les intrants à la main.
            </span>
          </div>
        ) : null}

        {/* Intrants manuels (4) — en haut, bien visibles */}
        <SubCard icon={Coins} title="Intrants de l'investisseur" cols={4}>
          <FieldNumber
            label="Capital total à injecter"
            value={inputs.capital}
            onSave={(v) => setField("capital", v)}
            format="money"
          />
          <FieldNumber
            label="% détenu par l'investisseur"
            value={inputs.pct * 100}
            onSave={(v) => setField("pct", v == null ? null : v / 100)}
            format="percent"
          />
          <FieldNumber
            label="Croissance annuelle des loyers"
            value={inputs.cr_loyers * 100}
            onSave={(v) => setField("cr_loyers", v == null ? null : v / 100)}
            format="percent"
          />
          <FieldNumber
            label="Croissance annuelle des dépenses"
            value={inputs.cr_dep * 100}
            onSave={(v) => setField("cr_dep", v == null ? null : v / 100)}
            format="percent"
          />
        </SubCard>

        {/* Intrants repris de l'analyse (8) — repliable */}
        <div className="mt-3 rounded-xl border border-brand-800 bg-brand-950/40">
          <button
            type="button"
            onClick={() => setAutoOpen((o) => !o)}
            className="flex w-full items-center justify-between gap-2 px-3.5 py-3 text-left"
          >
            <span className="flex items-center gap-2">
              <ListChecks className="h-3.5 w-3.5 text-accent-500" />
              <span className="text-xs font-semibold text-white/80">
                Repris automatiquement de l&apos;analyse
              </span>
              <span className="text-[10px] text-white/40">· modifiables si besoin</span>
            </span>
            <span className="text-[10px] uppercase tracking-wider text-white/40">
              {autoOpen ? "Masquer" : "Afficher"}
            </span>
          </button>
          {autoOpen ? (
            <div className="grid gap-3 px-3.5 pb-3.5 sm:grid-cols-2 lg:grid-cols-4">
              {TRI_AUTO_FIELDS.map((f) => {
                const isPct = f.format === "percent";
                const raw = (inputs[f.key] as number | undefined) ?? 0;
                return (
                  <FieldNumber
                    key={f.key}
                    label={f.label}
                    value={isPct ? raw * 100 : raw}
                    onSave={(v) =>
                      setField(f.key, v == null ? null : isPct ? v / 100 : v)
                    }
                    format={f.format === "number" ? undefined : f.format}
                  />
                );
              })}
            </div>
          ) : null}
        </div>

        {/* Bouton lancer le calcul */}
        {computeError ? (
          <p className="mt-3 text-xs text-rose-300">{computeError}</p>
        ) : null}
        <div className="mt-4 flex justify-end">
          <button
            type="button"
            onClick={() => void compute()}
            disabled={computing}
            className="btn-accent inline-flex items-center text-sm disabled:opacity-60"
          >
            {computing ? (
              <>
                <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
                Calcul en cours…
              </>
            ) : (
              <>
                <Flame className="mr-1.5 h-4 w-4" />
                Lancer le calcul
              </>
            )}
          </button>
        </div>
      </SectionCard>

      {/* Résultats */}
      {result ? (
        <TriResults result={result} />
      ) : (
        <EmptyTabHint
          icon={TrendingUp}
          message="Renseigne les intrants ci-dessus et lance le calcul pour obtenir le TRI par horizon de sortie."
        />
      )}
    </div>
  );
}

/** Bloc de résultats du TRI (vedette + métriques + tableaux). */
function TriResults({ result }: { result: TriResult }) {
  const [detailOpen, setDetailOpen] = useState(false);
  const TRI_HORIZONS = triHorizons(result);

  return (
    <div className="space-y-5">
      {/* EN VEDETTE : les 3 TRI par horizon de sortie */}
      <SectionCard
        icon={TrendingUp}
        title="Taux de rendement interne (TRI)"
        tone="emerald"
        subtitle="Rendement annualisé de l'investisseur selon l'année de sortie du deal."
      >
        <div className="grid gap-3 sm:grid-cols-3">
          {TRI_HORIZONS.map((h) => (
            <StatTile
              key={h.key}
              icon={Percent}
              label={`TRI — sortie ${h.label}`}
              value={_fmtTri(result.tri[h.tri])}
              tone="emerald"
            />
          ))}
        </div>
      </SectionCard>

      {/* Tableau « Par horizon de sortie » */}
      <SectionCard
        icon={TrendingUp}
        title="Par horizon de sortie"
        tone="neutral"
        subtitle="Cash retourné à l'investisseur et valeur de ses parts à chaque horizon."
      >
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-brand-800">
                <th className="px-2 py-2 text-left font-semibold text-white/60"></th>
                {TRI_HORIZONS.map((h) => (
                  <th
                    key={h.key}
                    className="px-2 py-2 text-right font-semibold text-white/70"
                  >
                    {h.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr className="border-t border-brand-800/60">
                <td className="px-2 py-2 text-white/60">
                  Cash retourné à l&apos;investisseur
                </td>
                {TRI_HORIZONS.map((h) => (
                  <td
                    key={h.key}
                    className="px-2 py-2 text-right font-mono tabular-nums text-emerald-300"
                  >
                    {fmtMoney(result.horizons[h.key].cash_investisseur)}
                  </td>
                ))}
              </tr>
              <tr className="border-t border-brand-800/60">
                <td className="px-2 py-2 text-white/60">Valeur des parts</td>
                {TRI_HORIZONS.map((h) => (
                  <td
                    key={h.key}
                    className="px-2 py-2 text-right font-mono tabular-nums text-white/90"
                  >
                    {fmtMoney(result.horizons[h.key].valeur_parts)}
                  </td>
                ))}
              </tr>
              {/* Patrimoine de l'investisseur à cet horizon = cash retourné
                  + valeur des parts. Mis en relief (gras). None-safe. */}
              <tr className="border-t-2 border-brand-700">
                <td className="px-2 py-2 font-bold text-white">Patrimoine</td>
                {TRI_HORIZONS.map((h) => {
                  const horizon = result.horizons[h.key];
                  const cash = horizon?.cash_investisseur;
                  const parts = horizon?.valeur_parts;
                  const patrimoine =
                    cash == null || parts == null ? null : cash + parts;
                  return (
                    <td
                      key={h.key}
                      className="px-2 py-2 text-right font-mono font-bold tabular-nums text-white"
                    >
                      {fmtMoney(patrimoine)}
                    </td>
                  );
                })}
              </tr>
            </tbody>
          </table>
        </div>
      </SectionCard>

      {/* Détail du calcul — repliable */}
      <div className="rounded-2xl border border-brand-800 bg-brand-900">
        <button
          type="button"
          onClick={() => setDetailOpen((o) => !o)}
          className="flex w-full items-center justify-between gap-3 px-5 py-4 text-left"
        >
          <span className="flex items-center gap-3">
            <span className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl bg-white/[0.06] text-white/60">
              <ListChecks className="h-5 w-5" />
            </span>
            <span>
              <span className="block text-base font-bold text-white">
                Détail du calcul
              </span>
              <span className="mt-0.5 block text-xs text-white/60">
                Projections par horizon + lignes de temps des flux.
              </span>
            </span>
          </span>
          <span className="text-[10px] uppercase tracking-wider text-white/40">
            {detailOpen ? "Masquer" : "Afficher"}
          </span>
        </button>

        {detailOpen ? (
          <div className="space-y-5 px-5 pb-5">
            <TriDetailTable result={result} />
            <TriFluxTable result={result} />
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** Tableau dense des projections par horizon (type « détails des calculs »). */
function TriDetailTable({ result }: { result: TriResult }) {
  const TRI_HORIZONS = triHorizons(result);
  const rows: Array<{ label: string; pick: (h: TriHorizon) => string }> = [
    { label: "Loyers bruts", pick: (h) => fmtMoney(h.loyers) },
    { label: "Dépenses d'opération", pick: (h) => fmtMoney(h.depenses) },
    { label: "RNO (revenu net d'opération)", pick: (h) => fmtMoney(h.rno) },
    { label: "Valeur de l'immeuble", pick: (h) => fmtMoney(h.valeur_immeuble) },
    { label: "Prêt max au refinancement", pick: (h) => fmtMoney(h.pret_max_refi) },
    { label: "Argent disponible au refi", pick: (h) => fmtMoney(h.argent_dispo) },
    { label: "Équité", pick: (h) => fmtMoney(h.equite) },
    { label: "Retour de capital", pick: (h) => fmtMoney(h.retour_capital) },
    { label: "Surplus partagé", pick: (h) => fmtMoney(h.surplus) },
    {
      label: "Cash retourné à l'investisseur",
      pick: (h) => fmtMoney(h.cash_investisseur)
    },
    { label: "Valeur des parts", pick: (h) => fmtMoney(h.valeur_parts) }
  ];
  return (
    <div className="mt-1">
      <h4 className="text-[10px] font-semibold uppercase tracking-wider text-accent-500">
        Projections par horizon
      </h4>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-brand-800">
              <th className="px-2 py-1.5 text-left font-semibold text-white/60"></th>
              {TRI_HORIZONS.map((h) => (
                <th
                  key={h.key}
                  className="px-2 py-1.5 text-right font-semibold text-white/70"
                >
                  {h.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.label} className="border-t border-brand-800/60">
                <td className="px-2 py-1 text-white/60">{row.label}</td>
                {TRI_HORIZONS.map((h) => (
                  <td
                    key={h.key}
                    className="px-2 py-1 text-right font-mono tabular-nums text-white/90"
                  >
                    {row.pick(result.horizons[h.key])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Bases du calcul */}
      <h4 className="mt-4 text-[10px] font-semibold uppercase tracking-wider text-accent-500">
        Bases du calcul
      </h4>
      <table className="mt-2 w-full text-xs">
        <tbody>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">Hypothèque d&apos;achat</td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
              {fmtMoney(result.bases.hypotheque)}
            </td>
          </tr>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">Marge de manœuvre</td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
              {fmtMoney(result.bases.marge)}
            </td>
          </tr>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">
              RNO à l&apos;année du refi
            </td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
              {fmtMoney(result.bases.rno2)}
            </td>
          </tr>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">Multiplicateur de valeur</td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
              {result.bases.multiplicateur.toFixed(2)}
            </td>
          </tr>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">Cap rate</td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
              {_fmtPctFraction(result.bases.cap_rate, 2)}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

/** Lignes de temps des flux (3 séries an 0 → 12). */
function TriFluxTable({ result }: { result: TriResult }) {
  const TRI_HORIZONS = triHorizons(result);
  const dernier = Number(TRI_HORIZONS[TRI_HORIZONS.length - 1].key) || 12;
  const years = Array.from({ length: dernier + 1 }, (_, i) => i);
  return (
    <div className="mt-1">
      <h4 className="text-[10px] font-semibold uppercase tracking-wider text-accent-500">
        Lignes de temps des flux (scénario de sortie)
      </h4>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-[11px]">
          <thead>
            <tr className="border-b border-brand-800">
              <th className="px-2 py-1.5 text-left font-semibold text-white/60">
                Scénario de sortie
              </th>
              {years.map((y) => (
                <th
                  key={y}
                  className="px-2 py-1.5 text-right font-semibold text-white/60"
                >
                  {y}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {TRI_HORIZONS.map((h) => (
              <tr key={h.key} className="border-t border-brand-800/60">
                <td className="px-2 py-1 whitespace-nowrap text-white/70">
                  {h.label}
                </td>
                {result.flux[h.key].map((f, i) => (
                  <td
                    key={i}
                    className={`px-2 py-1 text-right font-mono tabular-nums ${
                      f > 0
                        ? "text-emerald-300"
                        : f < 0
                        ? "text-rose-300"
                        : "text-white/30"
                    }`}
                  >
                    {f === 0 ? "—" : fmtMoney(f)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── Vignette d'attachment : fetch via authedFetch puis blob URL ───

function AttachmentThumb({
  leadId,
  attachment
}: {
  leadId: number;
  attachment: {
    id: number;
    filename: string;
    content_type: string;
    size_bytes: number;
  };
}) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [err, setErr] = useState(false);
  const isImage = (attachment.content_type || "").startsWith("image/");
  const isPdf = (attachment.content_type || "").includes("pdf");

  useEffect(() => {
    let cancelled = false;
    let cleanupUrl: string | null = null;
    (async () => {
      try {
        const r = await authedFetch(
          `/api/v1/lead-analyses/${leadId}/attachments/${attachment.id}`
        );
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const blob = await r.blob();
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        cleanupUrl = url;
        setBlobUrl(url);
      } catch {
        if (!cancelled) setErr(true);
      }
    })();
    return () => {
      cancelled = true;
      if (cleanupUrl) URL.revokeObjectURL(cleanupUrl);
    };
  }, [leadId, attachment.id]);

  return (
    <a
      href={blobUrl || "#"}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => {
        if (!blobUrl) e.preventDefault();
      }}
      className="block overflow-hidden rounded-md border border-brand-800 bg-brand-950 hover:border-accent-500"
      title={attachment.filename}
    >
      {isImage && blobUrl ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={blobUrl}
          alt={attachment.filename}
          className="h-24 w-full object-cover"
        />
      ) : isImage && !blobUrl && !err ? (
        <div className="flex h-24 items-center justify-center text-white/30">
          <Loader2 className="h-4 w-4 animate-spin" />
        </div>
      ) : err ? (
        <div className="flex h-24 items-center justify-center text-2xl text-rose-300/60">
          ✗
        </div>
      ) : isPdf ? (
        <div className="flex h-24 items-center justify-center text-3xl text-white/30">
          📕
        </div>
      ) : (
        <div className="flex h-24 items-center justify-center text-3xl text-white/30">
          📄
        </div>
      )}
      <p className="truncate px-2 py-1 text-[10px] text-white/60">
        {attachment.filename}
      </p>
    </a>
  );
}

// ─── Champs éditables ────────────────────────────────────────────

const CHAMPS_NECESSAIRES_CALC: ReadonlySet<string> = new Set([
  "asking_price",
  "nb_logements",
  "revenus_bruts",
  "taxes_municipales",
  "taxes_scolaires",
  "assurances",
  "energie",
  "typology_json"
]);

function FieldText({
  label,
  value,
  onSave,
  required,
  name
}: {
  label: string;
  value: string | null;
  onSave: (v: string | null) => void;
  required?: boolean;
  name?: string;
}) {
  const [v, setV] = useState(value || "");
  useEffect(() => setV(value || ""), [value]);
  const isEmpty = !value;
  const missingRequired = isEmpty && required;
  const necessaryForCalc =
    isEmpty && !missingRequired && !!name && CHAMPS_NECESSAIRES_CALC.has(name);
  return (
    <div>
      <label
        className={`text-[10px] uppercase tracking-wider ${
          missingRequired
            ? "text-rose-400 font-semibold"
            : necessaryForCalc
            ? "text-amber-300/80"
            : "text-white/50"
        }`}
      >
        {label}
        {missingRequired ? " · OBLIGATOIRE" : ""}
      </label>
      <input
        type="text"
        value={v}
        onChange={(e) => setV(e.target.value)}
        onBlur={() => {
          if ((value || "") !== v) onSave(v.trim() || null);
        }}
        className={`input mt-1 text-xs ${
          missingRequired
            ? "border-rose-400/70 focus:border-rose-400 ring-1 ring-rose-400/30"
            : ""
        }`}
      />
    </div>
  );
}

/** Alias historique — délègue au formatage monétaire unifié. */
function _formatMoneyExcel(n: number): string {
  return fmtMoney(n);
}

function _formatPercentExcel(n: number): string {
  return `${n.toFixed(2)} %`;
}

function _parseNumberLiberal(s: string): number | null {
  if (s == null) return null;
  const cleaned = s
    .replace(/[\s ]/g, "")
    .replace(/\$/g, "")
    .replace(/%/g, "")
    .replace(/,/g, ".");
  if (cleaned === "" || cleaned === "-" || cleaned === ".") return null;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : null;
}

function FieldNumber({
  label,
  value,
  onSave,
  required,
  onEstimate,
  estimating,
  format = "plain",
  name
}: {
  label: string;
  value: number | null;
  onSave: (v: number | null) => void;
  required?: boolean;
  onEstimate?: () => void;
  estimating?: boolean;
  format?: "money" | "percent" | "plain";
  name?: string;
}) {
  const [focused, setFocused] = useState(false);
  const [v, setV] = useState(value != null ? String(value) : "");
  useEffect(() => {
    if (!focused) setV(value != null ? String(value) : "");
  }, [value, focused]);
  const isEmpty = value == null;
  const missingRequired = isEmpty && required;
  const necessaryForCalc =
    isEmpty && !missingRequired && !!name && CHAMPS_NECESSAIRES_CALC.has(name);

  const displayed = (() => {
    if (focused) return v;
    if (value == null) return "";
    if (format === "money") return _formatMoneyExcel(value);
    if (format === "percent") return _formatPercentExcel(value);
    return String(value);
  })();

  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <label
          className={`text-[10px] uppercase tracking-wider ${
            missingRequired
              ? "text-rose-400 font-semibold"
              : necessaryForCalc
              ? "text-amber-300/80"
              : "text-white/50"
          }`}
        >
          {label}
          {missingRequired ? " · OBLIGATOIRE" : ""}
        </label>
        {isEmpty && onEstimate ? (
          <button
            type="button"
            onClick={onEstimate}
            disabled={!!estimating}
            className="inline-flex items-center gap-1 rounded border border-amber-400/50 bg-amber-500/15 px-1.5 py-0 text-[9px] font-semibold text-amber-200 hover:bg-amber-500/25 disabled:opacity-50"
            title="Estimer cette valeur avec l'IA"
          >
            {estimating ? (
              <Loader2 className="h-2.5 w-2.5 animate-spin" />
            ) : (
              <Sparkles className="h-2.5 w-2.5" />
            )}
            IA
          </button>
        ) : null}
      </div>
      <input
        type="text"
        inputMode="decimal"
        value={displayed}
        onFocus={(e) => {
          setFocused(true);
          setV(value != null ? String(value) : "");
          requestAnimationFrame(() => {
            try {
              e.target.select();
            } catch {
              /* ignore */
            }
          });
        }}
        onChange={(e) => setV(e.target.value)}
        onBlur={() => {
          setFocused(false);
          const num = _parseNumberLiberal(v);
          if (num !== value) onSave(num);
        }}
        className={`input mt-1 font-mono text-xs ${
          missingRequired
            ? "border-rose-400/70 focus:border-rose-400 ring-1 ring-rose-400/30"
            : ""
        }`}
      />
    </div>
  );
}

// ─── Typology editor ───────────────────────────────────────────

function TypologyEditor({
  value,
  onSave
}: {
  value: Record<string, number>;
  onSave: (newValue: Record<string, number>) => void;
}) {
  const [local, setLocal] = useState<Record<string, string>>(() => {
    const m: Record<string, string> = {};
    for (const k of TYPOLOGY_KEYS) {
      const n = Number(value[k] || 0);
      m[k] = n > 0 ? String(n) : "";
    }
    return m;
  });

  function commit(k: string, raw: string) {
    setLocal((prev) => ({ ...prev, [k]: raw }));
    const next: Record<string, number> = {};
    for (const kk of TYPOLOGY_KEYS) {
      const r = kk === k ? raw : local[kk];
      const n = Number(r);
      if (Number.isFinite(n) && n > 0) next[kk] = Math.floor(n);
    }
    onSave(next);
  }

  const total = TYPOLOGY_KEYS.reduce(
    (acc, k) => acc + (Number(local[k]) || 0),
    0
  );

  return (
    <div className="mt-1">
      <div className="grid grid-cols-4 gap-2 sm:grid-cols-8">
        {TYPOLOGY_KEYS.map((k) => (
          <div key={k} className="flex flex-col items-center">
            <label className="text-[10px] font-mono text-white/50">
              {k}
            </label>
            <input
              type="number"
              min="0"
              step="1"
              value={local[k]}
              onChange={(e) =>
                setLocal((prev) => ({ ...prev, [k]: e.target.value }))
              }
              onBlur={(e) => commit(k, e.target.value)}
              placeholder="0"
              className="w-full rounded border border-brand-800 bg-brand-950 px-1 py-0.5 text-center text-xs text-white focus:border-accent-500 focus:outline-none"
            />
          </div>
        ))}
      </div>
      <p className="mt-1 text-[10px] text-white/40">
        Total typologie : <strong className="text-white/70">{total}</strong>{" "}
        unité{total > 1 ? "s" : ""}
      </p>
    </div>
  );
}

// ─── Phase 3 — Unités & optimisation (chantier stratégies) ───────

type UniteRow = {
  typo: string;
  loyer_actuel: string;
  loyer_cible: string;
  optimiser: boolean;
};

function parseUnites(raw: string | null | undefined): UniteRow[] | null {
  if (!raw) return null;
  try {
    const j = JSON.parse(raw);
    if (!Array.isArray(j)) return null;
    return j.map((u) => ({
      typo: String(u.typo || ""),
      loyer_actuel:
        u.loyer_actuel != null ? String(u.loyer_actuel) : "",
      loyer_cible: u.loyer_cible != null ? String(u.loyer_cible) : "",
      optimiser: u.optimiser !== false
    }));
  } catch {
    return null;
  }
}

/** Choix des unités à optimiser (retour Phil 2026-08-31 : « cocher
 *  par unité »). Mode prêteur B : les non-cochées gardent leur loyer
 *  ACTUEL au refi. Mode direct : les cochées passent au loyer cible
 *  dès l'an 1, puis tout croît. */
function UnitesOptimisationCard({
  unitesJson,
  typology,
  prixLoyers,
  revenusBruts,
  nbLogements,
  defautOptimiser,
  onSave
}: {
  unitesJson: string | null | undefined;
  typology: Record<string, number>;
  prixLoyers: Record<string, string>;
  revenusBruts: number | null;
  nbLogements: number | null;
  /** true en prêteur B (tout coché), false en traditionnel. */
  defautOptimiser: boolean;
  onSave: (json: string | null) => void;
}) {
  const [rows, setRows] = useState<UniteRow[] | null>(() =>
    parseUnites(unitesJson)
  );
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    setRows(parseUnites(unitesJson));
    setDirty(false);
  }, [unitesJson]);

  const construire = useCallback((): UniteRow[] => {
    const actuelDefaut =
      revenusBruts && nbLogements
        ? (revenusBruts / 12 / nbLogements).toFixed(0)
        : "";
    const out: UniteRow[] = [];
    for (const k of Object.keys(typology).sort()) {
      const n = Number(typology[k]) || 0;
      for (let i = 0; i < n; i++) {
        out.push({
          typo: k,
          loyer_actuel: actuelDefaut,
          loyer_cible: prixLoyers[k] ?? "",
          optimiser: defautOptimiser
        });
      }
    }
    return out;
  }, [typology, prixLoyers, revenusBruts, nbLogements, defautOptimiser]);

  // Détail visible DIRECT, sans clic (retour Phil 2026-09-02) : dès
  // que la typologie existe et qu'aucune liste n'est enregistrée, on
  // génère et on sauvegarde pour que le moteur la voie.
  const autoFait = useRef(false);
  useEffect(() => {
    if (autoFait.current) return;
    if (unitesJson) return;
    const out = construire();
    if (out.length === 0) return;
    autoFait.current = true;
    onSave(
      JSON.stringify(
        out.map((r) => ({
          typo: r.typo,
          loyer_actuel: Number(r.loyer_actuel) || 0,
          loyer_cible: Number(r.loyer_cible) || 0,
          optimiser: r.optimiser
        }))
      )
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unitesJson, construire]);

  function generer() {
    setRows(construire());
    setDirty(true);
  }

  function maj(i: number, patch: Partial<UniteRow>) {
    setRows((rs) =>
      rs ? rs.map((r, j) => (j === i ? { ...r, ...patch } : r)) : rs
    );
    setDirty(true);
  }

  const totalActuel = (rows ?? []).reduce(
    (s, r) => s + (Number(r.loyer_actuel) || 0),
    0
  );
  const totalEffectif = (rows ?? []).reduce(
    (s, r) =>
      s +
      (r.optimiser
        ? Number(r.loyer_cible) || 0
        : Number(r.loyer_actuel) || 0),
    0
  );
  const nbOpt = (rows ?? []).filter((r) => r.optimiser).length;

  return (
    <SubCard icon={Gauge} title="Unités & optimisation" cols={2}>
      <div className="space-y-2 sm:col-span-2">
        <p className="text-[10px] leading-snug text-white/40">
          Coche les unités que tu optimises (loyer CIBLE au refi). Une
          unité décochée suit la croissance ORGANIQUE des revenus :
          loyer actuel × (1 + % par an) sur la durée du projet ou de
          la détention. Le loyer actuel par défaut = revenus bruts ÷
          nombre d&apos;unités — ajuste-le si tu connais le vrai prix
          de l&apos;unité.
        </p>
        {rows === null ? (
          <button
            type="button"
            onClick={generer}
            className="inline-flex items-center gap-1.5 rounded-lg border border-accent-500/40 bg-accent-500/10 px-3 py-1.5 text-xs font-semibold text-accent-300 transition hover:bg-accent-500/20"
          >
            Générer les unités depuis la typologie
          </button>
        ) : (
          <>
            <div className="overflow-x-auto rounded-lg border border-brand-800">
              <table className="w-full border-collapse text-[11px]">
                <thead>
                  <tr className="bg-brand-900 text-[9px] uppercase tracking-wider text-white/40">
                    <th className="px-2 py-1.5 text-left">Optimiser</th>
                    <th className="px-2 py-1.5 text-left">Unité</th>
                    <th className="px-2 py-1.5 text-right">
                      Loyer actuel ($/mois)
                    </th>
                    <th className="px-2 py-1.5 text-right">
                      Loyer cible ($/mois)
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-brand-800">
                  {rows.map((r, i) => (
                    <tr
                      key={i}
                      className={r.optimiser ? "" : "opacity-60"}
                    >
                      <td className="px-2 py-1">
                        <input
                          type="checkbox"
                          checked={r.optimiser}
                          onChange={(e) =>
                            maj(i, { optimiser: e.target.checked })
                          }
                          className="h-3.5 w-3.5 accent-emerald-500"
                        />
                      </td>
                      <td className="px-2 py-1 text-white/70">
                        {r.typo || "—"} · #{i + 1}
                      </td>
                      <td className="px-2 py-1 text-right">
                        <input
                          type="number"
                          step="any"
                          value={r.loyer_actuel}
                          onChange={(e) =>
                            maj(i, { loyer_actuel: e.target.value })
                          }
                          className="input w-24 py-0.5 text-right font-mono text-[11px]"
                        />
                      </td>
                      <td className="px-2 py-1 text-right">
                        <input
                          type="number"
                          step="any"
                          value={r.loyer_cible}
                          onChange={(e) =>
                            maj(i, { loyer_cible: e.target.value })
                          }
                          disabled={!r.optimiser}
                          className="input w-24 py-0.5 text-right font-mono text-[11px] disabled:opacity-40"
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-[10px] text-white/50">
              {nbOpt}/{rows.length} unités optimisées · actuel{" "}
              {fmtMoney(totalActuel)}/mois → effectif au refi{" "}
              {fmtMoney(totalEffectif)}/mois
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() =>
                  onSave(
                    JSON.stringify(
                      rows.map((r) => ({
                        typo: r.typo,
                        loyer_actuel: Number(r.loyer_actuel) || 0,
                        loyer_cible: Number(r.loyer_cible) || 0,
                        optimiser: r.optimiser
                      }))
                    )
                  )
                }
                disabled={!dirty}
                className="btn-accent px-3 py-1.5 text-xs disabled:opacity-50"
              >
                Enregistrer les unités
              </button>
              <button
                type="button"
                onClick={generer}
                className="rounded-lg border border-white/15 bg-white/5 px-3 py-1.5 text-xs font-semibold text-white/60 transition hover:bg-white/10"
              >
                Régénérer depuis la typologie
              </button>
              <button
                type="button"
                onClick={() => {
                  if (
                    window.confirm(
                      "Retirer le détail des unités ? L'analyse reviendra au comportement historique (tout au loyer cible pondéré)."
                    )
                  )
                    onSave(null);
                }}
                className="rounded-lg border border-rose-500/30 bg-rose-500/5 px-3 py-1.5 text-xs font-semibold text-rose-300/80 transition hover:bg-rose-500/10"
              >
                Retirer le détail
              </button>
            </div>
          </>
        )}
      </div>
    </SubCard>
  );
}

// ─── Section inputs analyse financière + bouton Lancer ──────────

function ManualAnalysisSection({
  data,
  onPatch,
  onRefresh
}: {
  data: LeadDetail;
  onPatch: (field: string, value: unknown) => void;
  onRefresh: () => Promise<void>;
}) {
  // Chantier « stratégies d'acquisition » (staging) : la porte s'ouvre
  // sur la version dev, ou si la fiche a déjà une stratégie choisie.
  const stratChantier =
    calculateurStrategiesActif() || !!data.strategie_acquisition;
  // Deux stratégies (retour Phil 2026-09-02) : « prêteur B » et
  // « institution traditionnelle » (les 4 programmes sur la même
  // page). Les anciennes valeurs conventionnel/schl_std/aph_50/
  // aph_100 sont des alias de traditionnel.
  const stratBrute = data.strategie_acquisition ?? "preteur_b";
  const strategie =
    stratBrute === "preteur_b" ? "preteur_b" : "traditionnel";
  const modePreteurB = stratChantier && strategie === "preteur_b";
  const modeDirect = stratChantier && strategie === "traditionnel";

  const typology = useMemo<Record<string, number>>(() => {
    if (!data.typology_json) return {};
    try {
      const j = JSON.parse(data.typology_json);
      if (j && typeof j === "object") return j;
    } catch {
      /* ignore */
    }
    return {};
  }, [data.typology_json]);

  const [prixLoyers, setPrixLoyers] = useState<Record<string, string>>(
    () => {
      try {
        const j = JSON.parse(data.loyers_projetes_json || "{}");
        const m: Record<string, string> = {};
        for (const k of Object.keys(j)) m[k] = String(j[k]);
        return m;
      } catch {
        return {};
      }
    }
  );

  const [loyerAbord, setLoyerAbord] = useState<string>(() => {
    try {
      const j = JSON.parse(data.loyers_max_abordabilite_json || "{}");
      return String(j.abordable ?? "");
    } catch {
      return "";
    }
  });

  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const missingRequired = useMemo(() => {
    const missing: string[] = [];
    if (!data.address) missing.push("Adresse");
    if (!data.city) missing.push("Ville");
    if (!data.nb_logements) missing.push("Nb logements");
    if (!data.asking_price) missing.push("Prix demandé");
    if (!data.revenus_bruts) missing.push("Revenus annuels");
    if (data.taxes_municipales == null) missing.push("Taxes municipales");
    if (data.taxes_scolaires == null) missing.push("Taxes scolaires");
    if (data.assurances == null) missing.push("Assurances");
    return missing;
  }, [data]);

  const missingRecommended = useMemo(() => {
    const missing: string[] = [];
    if (data.energie == null) missing.push("Énergie");
    if (data.depenses_autres == null) missing.push("Autres dépenses");
    if (!data.annee_construction) missing.push("Année construction");
    if (!data.evaluation_municipale) missing.push("Évaluation municipale");
    return missing;
  }, [data]);

  const missingEstimable = useMemo(() => {
    const m: string[] = [];
    if (data.taxes_municipales == null) m.push("Taxes municipales");
    if (data.taxes_scolaires == null) m.push("Taxes scolaires");
    if (data.assurances == null) m.push("Assurances");
    return m;
  }, [data]);

  const [estimating, setEstimating] = useState(false);

  async function estimateExpenses() {
    setEstimating(true);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/lead-analyses/${data.id}/estimate-expenses`,
        { method: "POST" }
      );
      if (!r.ok) {
        const txt = await r.text();
        throw new Error(txt.slice(0, 200) || `HTTP ${r.status}`);
      }
      const out = (await r.json()) as {
        taxes_municipales: number | null;
        taxes_scolaires: number | null;
        assurances: number | null;
        source: string;
        note?: string;
      };
      if (data.taxes_municipales == null && out.taxes_municipales != null) {
        onPatch("taxes_municipales", out.taxes_municipales);
      }
      if (data.taxes_scolaires == null && out.taxes_scolaires != null) {
        onPatch("taxes_scolaires", out.taxes_scolaires);
      }
      if (data.assurances == null && out.assurances != null) {
        onPatch("assurances", out.assurances);
      }
      await onRefresh();
    } catch (e) {
      setErr(`Estimation IA échouée : ${(e as Error).message}`);
    } finally {
      setEstimating(false);
    }
  }

  function setPrixLoyer(typo: string, v: string) {
    const next = { ...prixLoyers, [typo]: v };
    setPrixLoyers(next);
    const asJson: Record<string, number> = {};
    for (const [k, val] of Object.entries(next)) {
      const num = Number(val);
      if (Number.isFinite(num) && num > 0) asJson[k] = num;
    }
    onPatch("loyers_projetes_json", JSON.stringify(asJson));
  }

  function setLoyerAbordable(v: string) {
    setLoyerAbord(v);
    const num = Number(v);
    onPatch(
      "loyers_max_abordabilite_json",
      JSON.stringify(Number.isFinite(num) && num > 0 ? { abordable: num } : {})
    );
  }

  async function launchAnalysis() {
    setErr(null);
    if (missingRequired.length > 0) {
      setErr(
        `Champs obligatoires manquants : ${missingRequired.join(", ")}.`
      );
      return;
    }
    setRunning(true);
    try {
      const r = await authedFetch(
        `/api/v1/lead-analyses/${data.id}/run-financial-analysis`,
        { method: "POST" }
      );
      if (!r.ok) {
        const t = await r.text().catch(() => "");
        throw new Error(t.slice(0, 240) || `HTTP ${r.status}`);
      }
      await onRefresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  const loyersTypoKeys = TYPOLOGY_KEYS.filter((k) => (typology[k] || 0) > 0);

  return (
    <SectionCard
      icon={Calculator}
      title="Analyse financière — inputs manuels"
      tone="accent"
      subtitle="Paramètres du calcul. Les valeurs par défaut sont pré-remplies ; ajuste au besoin puis lance l'analyse."
    >
      {missingRequired.length > 0 ? (
        <div className="mb-4 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-[11px]">
          <p className="text-rose-300">
            ⚠ Obligatoires manquants :{" "}
            <strong>{missingRequired.join(", ")}</strong>. Complète-les
            dans l&apos;onglet « Infos » avant de lancer l&apos;analyse.
          </p>
          {missingEstimable.length > 0 ? (
            <div className="mt-1.5 flex items-center gap-2">
              <button
                type="button"
                onClick={() => void estimateExpenses()}
                disabled={estimating}
                className="inline-flex items-center gap-1.5 rounded-md border border-amber-400/50 bg-amber-500/15 px-2 py-1 text-[11px] font-semibold text-amber-200 hover:bg-amber-500/25 disabled:opacity-50"
                title="Estimer avec l'IA (taxes muni, taxes scol, assurances)"
              >
                {estimating ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Sparkles className="h-3 w-3" />
                )}
                Estimer avec l&apos;IA : {missingEstimable.join(", ")}
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      {missingRequired.length === 0 && missingRecommended.length > 0 ? (
        <p className="mb-4 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 text-[11px] text-white/60">
          ℹ Informations recommandées non saisies (l&apos;analyse peut
          quand même se lancer) :{" "}
          <strong className="text-white/80">
            {missingRecommended.join(", ")}
          </strong>
          .
        </p>
      ) : null}

      <div className="space-y-3">
        {/* Stratégie d'acquisition — chantier staging (2026-08-31).
            Visible seulement derrière la porte (ou si déjà choisie) :
            la prod garde l'affichage historique jusqu'au GO. */}
        {stratChantier ? (
          <SubCard icon={Percent} title="Stratégie d'acquisition" cols={2}>
            <div className="space-y-1.5 sm:col-span-2">
              {/* UN seul choix ici (retour Phil 2026-09-02) — le
                  programme d'achat retenu se choisit dans l'onglet
                  « Achat » (bouton Retenir), la référence de refi
                  dans « Refinancement ». */}
              <select
                value={strategie}
                onChange={(e) =>
                  onPatch("strategie_acquisition", e.target.value)
                }
                className="w-full rounded-lg border border-brand-700 bg-brand-950 px-2.5 py-2 text-sm text-white"
              >
                <option value="preteur_b">
                  Prêteur B + optimisation + refinancement rapide
                </option>
                <option value="traditionnel">
                  Institution traditionnelle (conventionnel / SCHL /
                  APH) + détention + refi
                </option>
              </select>
              <p className="text-[10px] text-white/40">
                {strategie === "traditionnel" ? (
                  <>
                    Les 4 programmes s&apos;affichent côte à côte dans
                    les onglets « Achat » (loyers actuels — choisis le
                    programme retenu là) et « Refinancement » (horizon
                    avec croissance — choisis ta référence là). PDF
                    inclus.
                  </>
                ) : (
                  <>
                    Achat au prêteur B, optimisation pendant la durée
                    du projet, refinancement SCHL — le mode actuel.
                    PDF inclus.
                  </>
                )}
              </p>
            </div>
          </SubCard>
        ) : null}

        {/* Financement & taux */}
        <SubCard icon={Percent} title="Financement & taux" cols={3}>
          <FieldNumber
            label="TGA (%)"
            value={data.tga_pct ?? 4}
            onSave={(v) => onPatch("tga_pct", v ?? 4)}
            format="percent"
          />
          {/* En stratégie prêteur B, le taux d'achat conventionnel ne
              sert à rien (retour Phil 2026-08-31) — masqué sur le
              chantier, conservé ailleurs. */}
          {!modePreteurB ? (
            <FieldNumber
              label="Taux intérêt achat (%)"
              value={data.taux_interet_achat_pct ?? 4}
              onSave={(v) => onPatch("taux_interet_achat_pct", v ?? 4)}
              format="percent"
            />
          ) : null}
          <FieldNumber
            label="Taux d'intérêt refi (%)"
            value={data.taux_interet_refi_pct}
            onSave={(v) => onPatch("taux_interet_refi_pct", v)}
            format="percent"
          />
          {!modeDirect ? (
            <FieldNumber
              label="MDF prêteur B (%)"
              value={data.mdf_preteur_b_pct ?? 25}
              onSave={(v) => onPatch("mdf_preteur_b_pct", v ?? 25)}
              format="percent"
            />
          ) : null}
          {stratChantier && !modeDirect ? (
            <FieldNumber
              label="Prêt prêteur B (%)"
              value={100 - (data.mdf_preteur_b_pct ?? 25)}
              onSave={(v) =>
                onPatch(
                  "mdf_preteur_b_pct",
                  Math.min(100, Math.max(0, 100 - (v ?? 75)))
                )
              }
              format="percent"
            />
          ) : null}
          {!modeDirect ? (
            <FieldNumber
              label="Taux d'intérêt prêteur B (%)"
              value={data.taux_interet_preteur_b_projet_pct ?? 8}
              onSave={(v) =>
                onPatch("taux_interet_preteur_b_projet_pct", v ?? 8)
              }
              format="percent"
            />
          ) : null}
          {!modeDirect ? (
            <FieldNumber
              label="Durée projet (années)"
              value={data.duree_projet_annees}
              onSave={(v) => onPatch("duree_projet_annees", v)}
            />
          ) : null}
          {modeDirect ? (
            <FieldNumber
              label="Horizon de détention (années)"
              value={data.projection_horizon_annees ?? 5}
              onSave={(v) =>
                onPatch("projection_horizon_annees", v ?? 5)
              }
            />
          ) : null}
          {/* Croissances organiques (retour Phil 2026-09-02) : dans la
              saisie du haut, pour les DEUX stratégies — elles indexent
              les loyers des unités non optimisées et les dépenses
              réelles au refi/pendant la détention. */}
          {stratChantier ? (
            <>
              <FieldNumber
                label="Croissance revenus organique (%/an)"
                value={(data.tri_croissance_loyers ?? 0.03) * 100}
                onSave={(v) =>
                  onPatch("tri_croissance_loyers", (v ?? 3) / 100)
                }
                format="percent"
              />
              <FieldNumber
                label="Croissance dépenses (%/an)"
                value={(data.tri_croissance_depenses ?? 0.03) * 100}
                onSave={(v) =>
                  onPatch("tri_croissance_depenses", (v ?? 3) / 100)
                }
                format="percent"
              />
            </>
          ) : null}
          {stratChantier ? (
            <>
              <FieldNumber
                label="Balance de vente ($)"
                value={data.balance_vente_montant}
                onSave={(v) => onPatch("balance_vente_montant", v)}
                format="money"
              />
              <FieldNumber
                label="Taux balance de vente (%)"
                value={data.balance_vente_taux_pct}
                onSave={(v) => onPatch("balance_vente_taux_pct", v)}
                format="percent"
              />
              {/* Cashback (retour Phil 2026-09-04) : le prix déclaré à
                  l'institution = prix + cashback → plus gros prêt ; le
                  cashback reçu au notaire réduit le cash, sans intérêt. */}
              <FieldNumber
                label="Cashback reçu au notaire ($)"
                value={data.cashback_montant ?? null}
                onSave={(v) => onPatch("cashback_montant", v)}
                format="money"
              />
            </>
          ) : null}
        </SubCard>

        {/* Optimisation refi — sans objet en achat direct (le choix
            des unités à optimiser arrive en phase 3). */}
        {modeDirect ? null : (
        <SubCard icon={Gauge} title="Optimisation refi" cols={4}>
          <FieldNumber
            label="Logements ajoutés refi"
            value={data.nb_logements_ajoutes}
            onSave={(v) => onPatch("nb_logements_ajoutes", v)}
          />
          <FieldNumber
            label="Thermopompes ajoutées"
            value={data.nb_thermopompes_ajoutees}
            onSave={(v) => onPatch("nb_thermopompes_ajoutees", v)}
          />
          <FieldNumber
            label="% réduction énergie"
            value={data.reduction_energie_pct}
            onSave={(v) => onPatch("reduction_energie_pct", v)}
            format="percent"
          />
          <FieldYesNo
            label="Wifi inclus refi"
            value={data.ajout_wifi ?? true}
            onSave={(v) => onPatch("ajout_wifi", v)}
          />
        </SubCard>
        )}

        {/* Frais & projet */}
        <SubCard icon={Banknote} title="Frais & projet" cols={4}>
          <FieldNumber
            label="Frais développement ($)"
            value={data.frais_developpement}
            onSave={(v) => onPatch("frais_developpement", v)}
            format="money"
          />
          <FieldNumber
            label="Frais négociations ($)"
            value={data.frais_negociations}
            onSave={(v) => onPatch("frais_negociations", v)}
            format="money"
          />
          <FieldNumber
            label="Frais travaux ($)"
            value={data.travaux_estimes}
            onSave={(v) => onPatch("travaux_estimes", v)}
            format="money"
          />
          <FieldNumber
            label="Loyer abordable (APH SELECT)"
            value={loyerAbord ? Number(loyerAbord) : null}
            onSave={(v) => setLoyerAbordable(v == null ? "" : String(v))}
          />
        </SubCard>

        {/* Loyers projetés par typologie — sert aussi de loyer CIBLE
            par défaut des unités (les deux stratégies). */}
        <SubCard icon={Coins} title="Loyers projetés par typologie" cols={3}>
          {loyersTypoKeys.map((k) => (
            <div key={k}>
              <label className="text-[10px] uppercase tracking-wider text-white/50">
                {k} ({typology[k]} log.) — $/mois
              </label>
              <input
                type="number"
                step="any"
                value={prixLoyers[k] ?? ""}
                onChange={(e) => setPrixLoyer(k, e.target.value)}
                className="input mt-1 font-mono text-xs"
                placeholder="ex. 1400"
              />
            </div>
          ))}
          {loyersTypoKeys.length === 0 ? (
            <p className="col-span-full text-[11px] text-white/40">
              Renseigne d&apos;abord la typologie dans l&apos;onglet
              « Infos ».
            </p>
          ) : null}
        </SubCard>

        {/* Optimisation par unité — APRÈS la typologie (retour Phil
            2026-09-02), détail visible direct, défaut selon la
            stratégie : tout coché en prêteur B, rien en
            traditionnel. */}
        {stratChantier ? (
          <UnitesOptimisationCard
            unitesJson={data.unites_json}
            typology={typology}
            prixLoyers={prixLoyers}
            revenusBruts={data.revenus_bruts}
            nbLogements={data.nb_logements}
            defautOptimiser={strategie === "preteur_b"}
            onSave={(json) => onPatch("unites_json", json)}
          />
        ) : null}

        {/* Composition de la mise de fonds — c'est de la SAISIE
            (montants modifiables + coches finançables), donc dans
            l'onglet Analyse (retour Phil 2026-09-02). Les deux modes
            partagent le même tableau. */}
        {(() => {
          if (!data.analysis_results_json) return null;
          let res: AnalysisResults | null = null;
          try {
            res = JSON.parse(
              data.analysis_results_json
            ) as AnalysisResults;
          } catch {
            res = null;
          }
          if (!res) return null;
          const trad = res.traditionnel;
          return (
            <FraisDemarrageBreakdownPanel
              data={res}
              overridesJson={data.frais_demarrage_overrides_json}
              financablesJson={data.frais_demarrage_financables_json}
              mdfPct={data.mdf_preteur_b_pct ?? 25}
              prixAchat={data.asking_price ?? 0}
              mdfPreteurBDb={data.mdf_preteur_b ?? null}
              onPatchOverrides={(j) =>
                onPatch("frais_demarrage_overrides_json", j)
              }
              onPatchFinancables={(j) =>
                onPatch("frais_demarrage_financables_json", j)
              }
              mode={trad ? "traditionnel" : "preteur_b"}
              chantier={stratChantier}
              dureeProjet={data.duree_projet_annees}
              pretRetenu={trad?.pret_retenu}
              programmeLabel={
                trad
                  ? trad.labels[trad.programme_retenu]
                  : undefined
              }
            />
          );
        })()}
      </div>

      {err ? (
        <p className="mt-4 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-300">
          {err}
        </p>
      ) : null}

      <div className="mt-4 flex justify-end">
        <button
          type="button"
          onClick={() => void launchAnalysis()}
          disabled={running || missingRequired.length > 0}
          className="btn-accent inline-flex items-center text-sm disabled:opacity-60"
        >
          {running ? (
            <>
              <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
              Calcul en cours…
            </>
          ) : (
            <>
              <Flame className="mr-1.5 h-4 w-4" />
              Lancer l&apos;analyse
            </>
          )}
        </button>
      </div>
    </SectionCard>
  );
}

function FieldYesNo({
  label,
  value,
  onSave
}: {
  label: string;
  value: boolean;
  onSave: (v: boolean) => void;
}) {
  // Structure alignée sur `FieldNumber` : label uppercase au-dessus,
  // contrôle pleine largeur en dessous à la même hauteur qu'un `.input`
  // pour un alignement parfait dans la grille `SubCard`. Le contrôle est
  // un segmented « Oui / Non » avec les deux options toujours visibles et
  // un fort contraste sur l'option active.
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <label className="text-[10px] uppercase tracking-wider text-white/50">
          {label}
        </label>
      </div>
      <div
        role="radiogroup"
        aria-label={label}
        className="mt-1 grid h-[38px] grid-cols-2 gap-1 rounded-lg border border-brand-700 bg-brand-950 p-1"
      >
        <button
          type="button"
          role="radio"
          aria-checked={value}
          onClick={() => onSave(true)}
          className={`flex items-center justify-center rounded-md text-xs font-semibold transition-colors ${
            value
              ? "bg-emerald-500 text-brand-950 shadow-sm"
              : "text-white/60 hover:bg-white/5 hover:text-white/80"
          }`}
        >
          Oui
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={!value}
          onClick={() => onSave(false)}
          className={`flex items-center justify-center rounded-md text-xs font-semibold transition-colors ${
            !value
              ? "bg-white/15 text-white"
              : "text-white/60 hover:bg-white/5 hover:text-white/80"
          }`}
        >
          Non
        </button>
      </div>
    </div>
  );
}

// ─── Types résultats ─────────────────────────────────────────────

type DepensesBreakdown = {
  inoccupation: number;
  taxes_municipales: number;
  taxes_scolaires: number;
  assurances: number;
  energie: number;
  concierge: number;
  entretien: number;
  gestion: number;
  wifi: number;
  thermopompes: number;
  autres: number;
  autres_normalisations?: number;
  interets_balance_vente?: number;
};

type ScenarioResult = {
  name: string;
  label: string;
  ltv: number;
  amort_annees: number;
  rcd: number;
  nb_log: number;
  loyer_mois: number;
  revenus_totaux: number;
  depenses_total: number;
  depenses?: DepensesBreakdown;
  revenus_net: number;
  valeur_eco_tga: number;
  valeur_eco_rcd: number;
  valeur_marchande: number | null;
  valeur_retenue: number;
  financement: number;
  paiement_mensuel_actuel?: number;
  cashflow_annuel?: number;
  mdf_necessaire: number | null;
  equite_a_la_fin: number | null;
};

type FraisDemarrageBreakdown = {
  courtier_hypothecaire_1: number;
  courtier_hypothecaire_2: number;
  interets_balance_vente?: number;
  detention?: number;
  taxes_bienvenue: number;
  evaluateur: number;
  evaluateur_2: number;
  inspection: number;
  avocat: number;
  notaire: number;
  notaire_2: number;
  rapport_efficacite: number;
  frais_developpement: number;
  frais_negociations: number;
  frais_travaux: number;
  // Mai 2026 : frais de dossier du prêteur B (2 % × prêt initial par
  // défaut, modifiable via le défaut global `frais_dossier_preteur_pct`).
  // Inséré APRÈS « Travaux estimés » dans l'UI.
  frais_dossier_preteur: number;
  interets: number;
  revenus_nets_pendant_projet: number;
};

type ProjPoint = {
  annee: number;
  revenus: number;
  depenses: number;
  rno: number;
  valeur: number;
  solde_pret: number;
  equite: number;
};

type AnalysisResults = {
  frais_demarrage?: FraisDemarrageBreakdown;
  frais_demarrage_total: number;
  prix_acquisition: number;
  mdf_preteur_b?: number;
  mdf_preteur_b_pct?: number;
  mdf_pct_prix_achat?: number;
  mdf_25pct_prix_achat?: number;
  prix_achat?: number;
  frais_demarrage_financables?: string[];
  taux_interet_preteur_b_projet?: number;
  taux_inoccupation_pct?: number;
  strategie?: string;
  pret_preteur_b?: {
    sur_prix: number;
    frais_finances: number;
    total: number;
  };
  balance_vente?: { montant: number; taux_pct: number };
  cashback?: { montant: number; prix_bancaire: number };
  traditionnel?: {
    programme_retenu: string;
    labels: Record<string, string>;
    horizon: number;
    croissance_loyers: number;
    croissance_depenses: number;
    balance_vente: number;
    interets_bv_annuels: number;
    cashback?: number;
    prix_bancaire?: number;
    frais_dossier_trad?: number;
    frais_demarrage: Record<string, number>;
    frais_demarrage_total: number;
    mdf_cash: number;
    pret_retenu: number;
    solde_retenu_an_h: number;
    achat: Record<string, ScenarioResult | null>;
    mdf_par_programme: Record<string, number>;
    refi: Record<string, ScenarioResult | null>;
    projection: ProjPoint[];
    total_depense?: number;
    frais_demarrage_cash?: number;
    programme_retenu_auto?: string;
    meilleur_refi_key?: string;
    best_refi: {
      key: string;
      label: string;
      argent_dispo: number;
      refi_possible: boolean;
      manque: number;
    };
  } | null;
  projection_preteur_b?: ProjPoint[] | null;
  typology: {
    h13_loyer_pondere: number;
    nb_abordables: number;
    nb_pdm: number;
    nouveau_loyer_moyen_pdm: number;
  };
  scenarios: {
    achat: ScenarioResult;
    refi_schl: ScenarioResult;
    refi_aph_50: ScenarioResult;
    refi_aph_100: ScenarioResult | null;
  };
  best_refi: {
    amount: number;
    program: string;
  };
};

/** Graphique de projection long terme — valeur de l'actif, solde du
 *  prêt et équité, avec grille et repère de l'horizon (v2, retour
 *  Phil : « améliore-le, rends-le plus beau »). */
function ProjectionChart({
  proj,
  horizon
}: {
  proj: ProjPoint[];
  horizon?: number;
}) {
  if (proj.length === 0) return null;
  const W = 680;
  const HT = 240;
  const padL = 58;
  const padR = 14;
  const padT = 16;
  const padB = 28;
  const a0 = proj[0].annee;
  const a1 = proj[proj.length - 1].annee;
  const vals = proj.flatMap((pt) => [pt.valeur, pt.solde_pret]);
  // Domaine Y SERRÉ autour des courbes (retour Phil 2026-09-02 : plus
  // de moitié de graphique vide), avec un peu d'air.
  const rawMin = Math.min(...vals);
  const rawMax = Math.max(...vals);
  const span = Math.max(rawMax - rawMin, 1);
  const yMin = Math.max(0, rawMin - span * 0.12);
  const yMax = rawMax + span * 0.08;
  const x = (a: number) =>
    padL + ((a - a0) / Math.max(a1 - a0, 1)) * (W - padL - padR);
  const y = (v: number) =>
    padT + (1 - (v - yMin) / (yMax - yMin)) * (HT - padT - padB);
  const line = (pick: (pt: ProjPoint) => number) =>
    proj
      .map(
        (pt, i) => `${i === 0 ? "M" : "L"}${x(pt.annee)},${y(pick(pt))}`
      )
      .join(" ");
  // Équité = aire entre valeur et solde.
  const area =
    line((pt) => pt.valeur) +
    proj
      .slice()
      .reverse()
      .map((pt) => `L${x(pt.annee)},${y(pt.solde_pret)}`)
      .join(" ") +
    " Z";
  const fmtK = (v: number) =>
    v >= 1_000_000
      ? `${(v / 1_000_000).toFixed(2).replace(".", ",")} M$`
      : `${Math.round(v / 1000)} k$`;
  const gridY = [0, 0.25, 0.5, 0.75, 1].map(
    (f) => yMin + (yMax - yMin) * f
  );
  const dernier = proj[proj.length - 1];
  return (
    <svg
      viewBox={`0 0 ${W} ${HT}`}
      className="w-full"
      role="img"
      aria-label="Projection : valeur de l'actif, solde du prêt et équité"
    >
      {gridY.map((v) => (
        <g key={v}>
          <line
            x1={padL}
            y1={y(v)}
            x2={W - padR}
            y2={y(v)}
            stroke="rgba(255,255,255,0.08)"
            strokeWidth="1"
          />
          <text
            x={padL - 6}
            y={y(v) + 3}
            textAnchor="end"
            fontSize="9"
            className="fill-white/40"
          >
            {fmtK(v)}
          </text>
        </g>
      ))}
      {/* Bande douce AVANT le refi : phase de détention initiale. */}
      {horizon != null && horizon > a0 && horizon < a1 ? (
        <rect
          x={padL}
          y={padT}
          width={x(horizon) - padL}
          height={HT - padT - padB}
          fill="rgba(255,255,255,0.025)"
        />
      ) : null}
      <path d={area} fill="rgba(52,211,153,0.14)" />
      {horizon != null && horizon > a0 && horizon < a1 ? (
        <g>
          <line
            x1={x(horizon)}
            y1={padT}
            x2={x(horizon)}
            y2={HT - padB}
            stroke="rgba(216,155,60,0.8)"
            strokeWidth="1.5"
            strokeDasharray="4 3"
          />
          <text
            x={x(horizon)}
            y={padT - 4}
            textAnchor="middle"
            fontSize="9"
            className="fill-amber-300"
          >
            refi an {horizon}
          </text>
        </g>
      ) : null}
      <path
        d={line((pt) => pt.valeur)}
        fill="none"
        stroke="#34d399"
        strokeWidth="2.4"
        strokeLinecap="round"
      />
      <path
        d={line((pt) => pt.solde_pret)}
        fill="none"
        stroke="#f59e0b"
        strokeWidth="2"
        strokeDasharray="6 4"
        strokeLinecap="round"
      />
      {proj.map((pt) => (
        <g key={pt.annee}>
          {/* Survol : les trois chiffres de l'année. */}
          <title>
            {`An ${pt.annee} — valeur ${fmtMoney(pt.valeur)} · solde ${fmtMoney(pt.solde_pret)} · équité ${fmtMoney(pt.equite)}`}
          </title>
          <rect
            x={x(pt.annee) - 10}
            y={padT}
            width={20}
            height={HT - padT - padB}
            fill="transparent"
          />
          <circle
            cx={x(pt.annee)}
            cy={y(pt.valeur)}
            r="2.6"
            fill="#34d399"
          />
          <circle
            cx={x(pt.annee)}
            cy={y(pt.solde_pret)}
            r="2.2"
            fill="#f59e0b"
          />
          <text
            x={x(pt.annee)}
            y={HT - 10}
            textAnchor="middle"
            fontSize="9"
            className="fill-white/40"
          >
            {pt.annee}
          </text>
        </g>
      ))}
      {/* Étiquettes de fin de courbe : lecture immédiate. */}
      <text
        x={x(a1) - 4}
        y={y(dernier.valeur) - 7}
        textAnchor="end"
        fontSize="10"
        fontWeight="bold"
        className="fill-emerald-300"
      >
        Valeur {fmtK(dernier.valeur)}
      </text>
      <text
        x={x(a1) - 4}
        y={y(dernier.solde_pret) + 14}
        textAnchor="end"
        fontSize="10"
        className="fill-amber-300"
      >
        Solde {fmtK(dernier.solde_pret)}
      </text>
    </svg>
  );
}

function ProjectionSection({
  proj,
  horizon,
  titre
}: {
  proj: ProjPoint[];
  horizon?: number;
  titre: string;
}) {
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-brand-800 bg-brand-950/40 p-3">
        <p className="text-[9px] uppercase tracking-wider text-white/40">
          {titre}
        </p>
        <ProjectionChart proj={proj} horizon={horizon} />
        <p className="mt-1 flex flex-wrap gap-3 text-[10px] text-white/50">
          <span className="text-emerald-300">
            — Valeur de l&apos;immeuble (RNO ÷ TGA)
          </span>
          <span className="text-amber-300">- - Solde du prêt</span>
          <span className="text-emerald-300/70">
            ▨ Équité (l&apos;écart entre les deux)
          </span>
        </p>
      </div>
      <div className="overflow-x-auto rounded-lg border border-brand-800">
        <table className="w-full border-collapse text-[10px]">
          <thead>
            <tr className="bg-brand-900 text-white/50">
              <th className="px-2 py-1.5 text-left text-[9px] font-semibold uppercase tracking-wider">
                Année
              </th>
              {proj.map((pt) => (
                <th
                  key={pt.annee}
                  className={`px-2 py-1.5 text-right ${
                    pt.annee === horizon ? "text-amber-300" : ""
                  }`}
                >
                  {pt.annee === horizon ? `★ ${pt.annee}` : pt.annee}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-brand-800">
            {(
              [
                ["Revenus", (pt) => pt.revenus],
                ["Dépenses", (pt) => pt.depenses],
                ["RNO", (pt) => pt.rno],
                ["Valeur", (pt) => pt.valeur],
                ["Solde prêt", (pt) => pt.solde_pret],
                ["Équité", (pt) => pt.equite]
              ] as Array<[string, (pt: ProjPoint) => number]>
            ).map(([label, pick]) => (
              <tr key={label}>
                <td className="whitespace-nowrap px-2 py-1 text-white/60">
                  {label}
                </td>
                {proj.map((pt) => (
                  <td
                    key={pt.annee}
                    className={`px-2 py-1 text-right font-mono tabular-nums ${
                      pt.annee === horizon
                        ? "bg-amber-500/5 text-white/90"
                        : "text-white/70"
                    }`}
                  >
                    {fmtMoney(pick(pt))}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Tableau 4 programmes (mêmes lignes que le tableau prêteur B) pour
 *  les onglets Achat et Refinancement du mode traditionnel. */
function TradColonnes({
  cols,
  labels,
  surligne,
  meilleur,
  mdfParProgramme,
  modeRefi,
  totalDepense,
  onRetenir
}: {
  cols: Record<string, ScenarioResult | null>;
  labels: Record<string, string>;
  surligne: string;
  /** Le meilleur AUTOMATIQUE (étoile) — peut différer de la référence. */
  meilleur?: string;
  mdfParProgramme?: Record<string, number>;
  modeRefi?: boolean;
  /** Refi : « Total dépensé » (prêt initial + tout le cash) — même
   *  valeur pour toutes les colonnes, affichée sous Prêt accordé. */
  totalDepense?: number;
  /** Achat : bouton « Retenir » par colonne. */
  onRetenir?: (k: string) => void;
}) {
  const ordre = ["conventionnel", "schl_std", "aph_50", "aph_100"];
  const keys = ordre.filter((k) => cols[k]);
  const rows: Array<{
    label: string;
    pick: (s: ScenarioResult, k: string) => number | null | undefined;
    bold?: boolean;
    color?: boolean;
  }> = [
    { label: "Loyer moyen ($/mois)", pick: (s) => s.loyer_mois },
    { label: "Revenus totaux ($/an)", pick: (s) => s.revenus_totaux },
    { label: "Dépenses totales", pick: (s) => s.depenses_total },
    { label: "Revenus net", pick: (s) => s.revenus_net },
    { label: "Valeur éco RDC", pick: (s) => s.valeur_eco_rcd },
    { label: "Valeur éco TGA", pick: (s) => s.valeur_eco_tga },
    { label: "Valeur retenue", pick: (s) => s.valeur_retenue, bold: true },
    { label: "Prêt accordé", pick: (s) => s.financement, bold: true },
    ...(modeRefi
      ? [
          {
            label: "Total dépensé (prêt initial + cash)",
            pick: () => totalDepense,
            bold: false
          },
          {
            label: "Argent dégagé (prêt − total dépensé)",
            pick: (s: ScenarioResult) => s.equite_a_la_fin,
            bold: true,
            color: true
          }
        ]
      : [
          {
            label: "MDF nécessaire (cash)",
            pick: (_s: ScenarioResult, k: string) =>
              mdfParProgramme?.[k],
            bold: true
          },
          {
            label: "Cashflow annuel",
            pick: (s: ScenarioResult) => s.cashflow_annuel,
            bold: true,
            color: true
          }
        ])
  ];
  return (
    <div className="overflow-x-auto rounded-xl border border-brand-800">
      <table className="w-full border-collapse text-[11px]">
        <thead>
          <tr className="bg-brand-900 align-bottom text-white/50">
            <th className="px-2.5 py-2.5 text-left text-[9px] font-semibold uppercase tracking-wider text-white/40">
              Métrique
            </th>
            {keys.map((k) => (
              <th
                key={k}
                className={`px-2.5 py-2.5 text-right align-bottom font-semibold ${
                  k === surligne ? "bg-emerald-500/10" : "bg-brand-900"
                }`}
              >
                {k === surligne ? (
                  <span className="mb-1 inline-flex items-center gap-1 rounded-full bg-emerald-500/20 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider text-emerald-300">
                    {modeRefi ? "Référence" : "Retenu"}
                  </span>
                ) : null}
                <span
                  className={`block text-[11px] ${
                    k === surligne ? "text-emerald-200" : "text-white/80"
                  }`}
                >
                  {meilleur === k ? "★ " : ""}
                  {labels[k] || k}
                </span>
                {onRetenir && k !== surligne ? (
                  <button
                    type="button"
                    onClick={() => onRetenir(k)}
                    className="mt-1 rounded-md border border-white/15 bg-white/5 px-2 py-0.5 text-[9px] font-semibold text-white/60 transition hover:bg-white/10 hover:text-white"
                    title="Utiliser ce programme pour financer l'achat (pilote la MDF et le solde au refi)"
                  >
                    Retenir
                  </button>
                ) : null}
                <span className="block text-[9px] font-normal text-white/40">
                  LTV {((cols[k]?.ltv ?? 0) * 100).toFixed(0)} % ·{" "}
                  {cols[k]?.amort_annees} ans · RCD{" "}
                  {(cols[k]?.rcd ?? 0).toFixed(2)}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-brand-800">
          {rows.map((r) => (
            <tr key={r.label}>
              <td className="px-2.5 py-1.5 text-white/60">{r.label}</td>
              {keys.map((k) => {
                const s = cols[k];
                const v = s ? r.pick(s, k) : null;
                return (
                  <td
                    key={k}
                    className={`px-2.5 py-1.5 text-right font-mono tabular-nums ${
                      k === surligne ? "bg-emerald-500/5" : ""
                    } ${
                      r.color && v != null
                        ? v >= 0
                          ? "text-emerald-300"
                          : "text-rose-300"
                        : r.bold
                          ? "font-bold text-white/90"
                          : "text-white/70"
                    }`}
                  >
                    {v == null ? "—" : fmtMoney(v)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const REFI_LABELS_B: Array<[string, string]> = [
  ["refi_schl", "SCHL standard"],
  ["refi_aph_50", "SCHL Efficacité (50 pts)"],
  ["refi_aph_100", "SCHL Abordabilité + Efficacité (100 pts)"]
];

/** Sélecteur de la RÉFÉRENCE de refinancement (retour Phil
 *  2026-09-02 : le meilleur est proposé, mais je peux choisir). */
function RefiReferenceSelect({
  options,
  value,
  meilleurLabel,
  onChange
}: {
  options: Array<[string, string]>;
  value: string | null | undefined;
  meilleurLabel: string;
  onChange: (v: string | null) => void;
}) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
      <span className="text-white/50">Référence des calculs :</span>
      <select
        value={value || ""}
        onChange={(e) => onChange(e.target.value || null)}
        className="rounded-lg border border-brand-700 bg-brand-950 px-2 py-1.5 text-xs text-white"
        title="Le verdict, la projection et la carte kanban suivent cette référence"
      >
        <option value="">Automatique — meilleur ({meilleurLabel})</option>
        {options.map(([k, l]) => (
          <option key={k} value={k}>
            {l}
          </option>
        ))}
      </select>
    </div>
  );
}

/** Onglet ACHAT — institution traditionnelle : 4 programmes sur les
 *  loyers actuels, choix du programme retenu, composition de la MDF. */
function TraditionnelAchatPanel({
  t,
  programmeChoisi,
  onPatchField
}: {
  t: NonNullable<AnalysisResults["traditionnel"]>;
  /** Choix EXPLICITE de la fiche (null = automatique, prêt le plus
   *  élevé). */
  programmeChoisi?: string | null;
  onPatchField?: (field: string, value: unknown) => void;
}) {
  return (
    <SectionCard
      icon={Banknote}
      title="Achat — Institution traditionnelle"
      tone="emerald"
      subtitle={
        <>
          Financé sur les loyers et dépenses ACTUELS (plafonné au prix
          demandé). Le programme RETENU pilote la mise de fonds et le
          solde du prêt au refinancement — clique « Retenir » pour en
          changer.
        </>
      }
      action={
        <div className="rounded-lg border border-amber-400/40 bg-amber-500/10 px-3 py-1.5 text-right">
          <p className="text-[9px] uppercase tracking-wider text-amber-300/80">
            Mise de fonds (cash)
          </p>
          <p className="font-mono text-sm font-bold tabular-nums text-amber-200">
            {fmtMoney(t.mdf_cash)}
          </p>
          <p className="text-[10px] text-white/50">
            {t.labels[t.programme_retenu]}
          </p>
        </div>
      }
    >
      {onPatchField ? (
        <RefiReferenceSelect
          options={[
            ["conventionnel", t.labels.conventionnel],
            ["schl_std", t.labels.schl_std],
            ["aph_50", t.labels.aph_50],
            ["aph_100", t.labels.aph_100]
          ]}
          value={programmeChoisi}
          meilleurLabel={`prêt le plus élevé : ${
            t.labels[t.programme_retenu_auto || ""] ||
            t.programme_retenu_auto ||
            "—"
          }`}
          onChange={(v) => onPatchField("programme_achat", v)}
        />
      ) : null}
      <TradColonnes
        cols={t.achat}
        labels={t.labels}
        surligne={t.programme_retenu}
        meilleur={t.programme_retenu_auto}
        mdfParProgramme={t.mdf_par_programme}
        onRetenir={
          onPatchField
            ? (k) => onPatchField("programme_achat", k)
            : undefined
        }
      />
      <p className="mt-2 text-[10px] text-white/40">
        ★ = prêt le plus élevé (référence automatique). Frais
        d&apos;acquisition {fmtMoney(t.frais_demarrage_total)}
        {t.frais_demarrage_cash != null &&
        t.frais_demarrage_cash !== t.frais_demarrage_total
          ? ` (dont ${fmtMoney(t.frais_demarrage_cash)} cash)`
          : ""}{" "}
        — sans double courtier/notaire ni intérêts de chantier ;
        courtier 1 sur le prêt, frais de dossier fixes.
        {t.cashback && t.cashback > 0
          ? ` Cashback ${fmtMoney(t.cashback)} reçu au notaire — prix déclaré à l'institution ${fmtMoney(t.prix_bancaire ?? 0)}.`
          : ""}
        {t.interets_bv_annuels > 0
          ? ` Intérêts de la balance de vente ${fmtMoney(t.interets_bv_annuels)}/an inclus dans les dépenses.`
          : ""}{" "}
        La composition détaillée de la mise de fonds s&apos;édite dans
        l&apos;onglet « Analyse ».
      </p>
    </SectionCard>
  );
}

/** Onglet REFINANCEMENT — institution traditionnelle : référence
 *  changeable, verdict, 4 programmes à l\'an H, projections. */
function TraditionnelRefiPanel({
  t,
  refiRetenu,
  onPatchField
}: {
  t: NonNullable<AnalysisResults["traditionnel"]>;
  refiRetenu?: string | null;
  onPatchField?: (field: string, value: unknown) => void;
}) {
  const best = t.best_refi;
  const meilleurKey = t.meilleur_refi_key || best.key;
  return (
    <SectionCard
      icon={TrendingUp}
      title={`Refinancement (an ${t.horizon}) — Institution traditionnelle`}
      tone="emerald"
      subtitle={
        <>
          Revenus et dépenses projetés à l&apos;an {t.horizon} (unités
          optimisées au loyer cible, les autres à la croissance
          organique {(t.croissance_loyers * 100).toFixed(1)} % ;
          dépenses réelles indexées{" "}
          {(t.croissance_depenses * 100).toFixed(1)} %/an).
        </>
      }
      action={
        <div
          className={`rounded-lg border px-3 py-1.5 text-right ${
            best.refi_possible
              ? "border-emerald-500/30 bg-emerald-500/10"
              : "border-rose-500/30 bg-rose-500/10"
          }`}
        >
          <p
            className={`text-[9px] uppercase tracking-wider ${
              best.refi_possible
                ? "text-emerald-300/80"
                : "text-rose-300/80"
            }`}
          >
            Argent dégagé (référence)
          </p>
          <p
            className={`font-mono text-sm font-bold tabular-nums ${
              best.refi_possible ? "text-emerald-300" : "text-rose-300"
            }`}
          >
            {fmtMoney(best.argent_dispo)}
          </p>
          <p className="text-[10px] text-white/50">{best.label}</p>
        </div>
      }
    >
      {onPatchField ? (
        <RefiReferenceSelect
          options={[
            ["conventionnel", t.labels.conventionnel],
            ["schl_std", t.labels.schl_std],
            ["aph_50", t.labels.aph_50],
            ["aph_100", t.labels.aph_100]
          ]}
          value={refiRetenu}
          meilleurLabel={t.labels[meilleurKey] || meilleurKey}
          onChange={(v) => onPatchField("refi_retenu", v)}
        />
      ) : null}

      <div
        className={`mb-3 rounded-lg border px-3 py-2 text-xs ${
          best.refi_possible
            ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
            : "border-rose-500/40 bg-rose-500/10 text-rose-200"
        }`}
      >
        {best.refi_possible ? (
          <>
            ✅ À l&apos;an {t.horizon}, le refinancement{" "}
            <strong>{best.label}</strong> dégage{" "}
            <strong>{fmtMoney(best.argent_dispo)}</strong> (après
            remboursement du prêt d&apos;achat
            {t.balance_vente > 0 ? " et de la balance de vente" : ""})
            — assez pour ressortir ta mise de fonds de{" "}
            {fmtMoney(t.mdf_cash)}.
          </>
        ) : (
          <>
            ⚠️ À l&apos;an {t.horizon}, le refinancement de référence (
            {best.label}) dégage {fmtMoney(best.argent_dispo)} — il
            manque <strong>{fmtMoney(best.manque)}</strong> pour
            ressortir ta mise de fonds de {fmtMoney(t.mdf_cash)}.
          </>
        )}
      </div>
      <TradColonnes
        cols={t.refi}
        labels={t.labels}
        surligne={best.key}
        meilleur={meilleurKey}
        modeRefi
        totalDepense={t.total_depense}
      />
      <p className="mt-2 text-[10px] text-white/40">
        Total dépensé = prêt initial + tout le cash (mise de fonds,
        frais, balance de vente) = {fmtMoney(t.total_depense)}. Argent
        dégagé = prêt max − total dépensé : à ce montant remboursé, il
        ne reste plus 1 $ à nous dans le projet. ★ = le meilleur
        automatique ; la colonne verte = ta référence.
      </p>

      <div className="mt-4">
        <ProjectionSection
          proj={t.projection}
          horizon={t.horizon}
          titre="Détention, refinancement, puis poursuite — valeur vs solde"
        />
      </div>
    </SectionCard>
  );
}

function AnalysisResultsTable({
  resultsJson,
  overridesJson,
  financablesJson,
  mdfPct,
  prixAchat,
  fraisDemarrageTotalDb,
  mdfPreteurBDb,
  onPatchOverrides,
  onPatchFinancables,
  onPatchField,
  refiRetenu
}: {
  resultsJson: string;
  overridesJson?: string | null;
  financablesJson?: string | null;
  mdfPct?: number;
  prixAchat?: number;
  fraisDemarrageTotalDb?: number | null;
  mdfPreteurBDb?: number | null;
  onPatchOverrides?: (json: string) => void;
  onPatchFinancables?: (json: string) => void;
  onPatchField?: (field: string, value: unknown) => void;
  refiRetenu?: string | null;
}) {
  void fraisDemarrageTotalDb;
  const data = useMemo<AnalysisResults | null>(() => {
    try {
      return JSON.parse(resultsJson) as AnalysisResults;
    } catch {
      return null;
    }
  }, [resultsJson]);

  if (!data) return null;

  // Phase 2 — stratégie « achat direct » : le tableau classique
  // (colonnes prêteur B/refi) ne s'applique pas, on affiche le
  // panneau dédié (achat + projection + verdict refi an N).
  if (data.traditionnel) {
    return (
      <TraditionnelRefiPanel
        t={data.traditionnel}
        refiRetenu={refiRetenu}
        onPatchField={onPatchField}
      />
    );
  }

  // Chantier stratégies (staging) : en mode prêteur B, la colonne
  // « Achat conventionnel » ne sert à rien (retour Phil 2026-08-31) —
  // le financement à l'achat est le prêt B, affiché dans la bande MDF.
  const masquerAchat =
    calculateurStrategiesActif() &&
    (data.strategie ?? "preteur_b") === "preteur_b";
  const cols: Array<[string, ScenarioResult | null]> = [
    ...(masquerAchat
      ? []
      : ([["Achat", data.scenarios.achat]] as Array<
          [string, ScenarioResult | null]
        >)),
    ["SCHL standard", data.scenarios.refi_schl],
    ["SCHL Efficacité (50 pts)", data.scenarios.refi_aph_50],
    ["SCHL Abord+Eff (100 pts)", data.scenarios.refi_aph_100]
  ];
  const refiStart = masquerAchat ? 0 : 1;

  // Identifie la colonne du scénario gagnant (= « Best refi ») pour la
  // mettre en valeur. Logique alignée sur la bande hero : on cherche
  // parmi les scénarios refi (indices 1→3) celui dont l'équité finale
  // correspond au montant best_refi, avec repli sur le libellé/nom du
  // programme. Purement visuel — n'altère aucun chiffre.
  const winnerIndex = (() => {
    const bestAmount = data.best_refi.amount;
    const bestProgram = data.best_refi.program;
    for (let i = refiStart; i < cols.length; i++) {
      const s = cols[i][1];
      if (
        s &&
        bestAmount != null &&
        s.equite_a_la_fin != null &&
        Math.abs(s.equite_a_la_fin - bestAmount) < 1
      ) {
        return i;
      }
    }
    if (bestProgram) {
      for (let i = refiStart; i < cols.length; i++) {
        const s = cols[i][1];
        if (s && (s.label === bestProgram || s.name === bestProgram)) return i;
      }
    }
    return -1;
  })();

  const jsonPct = data.mdf_preteur_b_pct;
  const jsonPctPercent = jsonPct != null && jsonPct < 1 ? jsonPct * 100 : jsonPct;
  const livePct = mdfPct ?? 25;
  const inputsChanged =
    jsonPctPercent != null && Math.abs(jsonPctPercent - livePct) > 0.01;

  const metricRows: Array<{
    label: string;
    pick: (s: ScenarioResult) => number | null | undefined;
    bold?: boolean;
    fallback?: string;
    colorEquite?: boolean;
    keyRow?: boolean;
  }> = [
    { label: "Loyer moyen ($/mois)", pick: (s) => s.loyer_mois },
    { label: "Revenus totaux ($/an)", pick: (s) => s.revenus_totaux },
    { label: "Dépenses totales", pick: (s) => s.depenses_total },
    { label: "Revenus net", pick: (s) => s.revenus_net },
    { label: "Valeur éco RDC", pick: (s) => s.valeur_eco_rcd },
    { label: "Valeur éco TGA", pick: (s) => s.valeur_eco_tga },
    { label: "Valeur marchande", pick: (s) => s.valeur_marchande, fallback: "—" },
    {
      label: "Valeur retenue",
      pick: (s) => s.valeur_retenue,
      bold: true,
      keyRow: true
    },
    {
      label: "Prêt accordé",
      pick: (s) => s.financement,
      bold: true,
      keyRow: true
    },
    { label: "MDF nécessaire", pick: (s) => s.mdf_necessaire, fallback: "N/A" },
    {
      label: "Cashflow annuel",
      pick: (s) => s.cashflow_annuel,
      fallback: "N/A",
      colorEquite: true,
      bold: true,
      keyRow: true
    },
    {
      label: "Équité à la fin",
      pick: (s) => s.equite_a_la_fin,
      fallback: "N/A",
      colorEquite: true,
      bold: true,
      keyRow: true
    }
  ];

  return (
    <SectionCard
      icon={TrendingUp}
      title="Résultats de l'analyse financière"
      tone="emerald"
      subtitle={
        <>
          Frais démarrage : {fmtMoney(data.frais_demarrage_total)} · Prix
          acquisition : {fmtMoney(data.prix_acquisition)} · Loyer pondéré
          H13 : {fmtMoney(data.typology.h13_loyer_pondere)} /mois
          {data.typology.nb_abordables > 0
            ? ` · ${data.typology.nb_abordables} abord / ${data.typology.nb_pdm} PDM`
            : ""}
        </>
      }
      action={
        <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-right">
          <p className="text-[9px] uppercase tracking-wider text-emerald-300/80">
            Best refi
          </p>
          <p className="font-mono text-sm font-bold tabular-nums text-emerald-300">
            {fmtMoney(data.best_refi.amount)}
          </p>
          <p className="text-[10px] text-white/50">{data.best_refi.program}</p>
        </div>
      }
    >
      {inputsChanged ? (
        <div className="mb-3 rounded-lg border border-amber-400/60 bg-amber-500/15 px-3 py-2 text-[11px] text-amber-200">
          ⚠ Les inputs ont changé depuis la dernière analyse
          (ex. MDF prêteur B : {jsonPctPercent}% → {livePct}%).{" "}
          <strong>Relance l&apos;analyse</strong> pour mettre à jour
          les résultats ci-dessous.
        </div>
      ) : null}

      {onPatchField && data.projection_preteur_b ? (
        <RefiReferenceSelect
          options={REFI_LABELS_B.filter(
            ([k]) =>
              (data.scenarios as Record<string, ScenarioResult | null>)[k]
          )}
          value={refiRetenu}
          meilleurLabel={(() => {
            let bk = "SCHL standard";
            let bv = -Infinity;
            for (const [k, l] of REFI_LABELS_B) {
              const s = (
                data.scenarios as Record<string, ScenarioResult | null>
              )[k];
              const e = s?.equite_a_la_fin;
              if (e != null && e > bv) {
                bv = e;
                bk = l;
              }
            }
            return bk;
          })()}
          onChange={(v) => onPatchField("refi_retenu", v)}
        />
      ) : null}

      {data.mdf_preteur_b != null ? (
        <div className="mb-3 rounded-lg border border-amber-400/40 bg-amber-500/10 px-3 py-2">
          <p className="text-[10px] uppercase tracking-wider text-amber-300">
            MDF avec prêteur B
          </p>
          <p className="mt-0.5 text-base font-bold text-amber-200">
            {fmtMoney(data.mdf_preteur_b)}
          </p>
          <p className="text-[10px] text-white/50">
            {(() => {
              const liveOrJson = mdfPct ?? data.mdf_preteur_b_pct ?? 25;
              const pctDisplay =
                liveOrJson < 1
                  ? (liveOrJson * 100).toFixed(0)
                  : liveOrJson.toFixed(0);
              const bv = data.balance_vente?.montant ?? 0;
              return bv > 0
                ? `${pctDisplay} % × prix d'achat − balance de vente (${fmtMoney(bv)}) + frais démarrage`
                : `${pctDisplay} % × prix d'achat + frais démarrage`;
            })()}
          </p>
        </div>
      ) : null}

      {/* Chantier stratégies (staging) — le prêt du prêteur B en
          miroir de la MDF (retour Phil 2026-08-31 : « j'aimerais y
          voir apparaître le prêt accordé par le prêteur B »). */}
      {masquerAchat && data.pret_preteur_b ? (
        <div className="mb-3 rounded-lg border border-sky-400/40 bg-sky-500/10 px-3 py-2">
          <p className="text-[10px] uppercase tracking-wider text-sky-300">
            Prêt accordé prêteur B
          </p>
          <p className="mt-0.5 text-base font-bold text-sky-200">
            {fmtMoney(data.pret_preteur_b.total)}
          </p>
          <p className="text-[10px] text-white/50">
            {fmtMoney(data.pret_preteur_b.sur_prix)} sur le prix
            {data.pret_preteur_b.frais_finances > 0
              ? ` + ${fmtMoney(data.pret_preteur_b.frais_finances)} de frais financés`
              : ""}
            {data.balance_vente && data.balance_vente.montant > 0
              ? ` · balance de vente ${fmtMoney(data.balance_vente.montant)} à ${data.balance_vente.taux_pct.toFixed(2)} %`
              : ""}
          </p>
        </div>
      ) : null}

      {/* Tableau desktop affiché en entier : aucun scroll (ni vertical ni
          horizontal). Les 4 colonnes tiennent dans la largeur du modal
          (max-w-5xl) et toutes les lignes sont visibles d'un coup. */}
      <div className="hidden rounded-xl border border-brand-800 sm:block">
        <table className="w-full table-fixed border-collapse text-[11px]">
          <thead>
            <tr className="bg-brand-900 align-bottom text-white/50">
              <th className="w-[28%] bg-brand-900 px-2.5 py-2.5 text-left text-[9px] font-semibold uppercase tracking-wider text-white/40">
                Métrique
              </th>
              {cols.map(([label, s], i) => {
                const isWinner = i === winnerIndex;
                return (
                  <th
                    key={label}
                    className={`px-2.5 py-2.5 text-right align-bottom font-semibold ${
                      isWinner
                        ? "bg-emerald-500/10"
                        : "bg-brand-900"
                    }`}
                  >
                    {isWinner ? (
                      <span className="mb-1 inline-flex items-center gap-1 rounded-full bg-emerald-500/20 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider text-emerald-300">
                        ★ Best
                      </span>
                    ) : null}
                    <span
                      className={`block text-[11px] ${
                        isWinner ? "text-emerald-200" : "text-white/80"
                      }`}
                    >
                      {label}
                    </span>
                    {s ? (
                      <span className="mt-0.5 block text-[9px] font-normal text-white/30">
                        {(s.ltv * 100).toFixed(0)}% · {s.amort_annees} ans · RCD{" "}
                        {s.rcd.toFixed(2)}
                      </span>
                    ) : null}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {metricRows.map((r) => (
              <ResultRow
                key={r.label}
                label={r.label}
                cols={cols}
                pick={r.pick}
                bold={r.bold}
                fallback={r.fallback}
                colorEquite={r.colorEquite}
                keyRow={r.keyRow}
                winnerIndex={winnerIndex}
              />
            ))}
          </tbody>
        </table>
      </div>

      {/* Fallback mobile : une carte par scénario */}
      <div className="space-y-3 sm:hidden">
        {cols.map(([label, s], i) => {
          const isWinner = i === winnerIndex;
          return (
            <div
              key={label}
              className={`rounded-xl border p-3 ${
                isWinner
                  ? "border-emerald-500/40 bg-emerald-500/[0.07]"
                  : "border-brand-800 bg-brand-950/40"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <p
                  className={`text-xs font-semibold ${
                    isWinner ? "text-emerald-200" : "text-white"
                  }`}
                >
                  {label}
                </p>
                {isWinner ? (
                  <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/20 px-2 py-0.5 text-[8px] font-bold uppercase tracking-wider text-emerald-300">
                    ★ Best
                  </span>
                ) : null}
              </div>
              {s ? (
                <>
                  <p className="mt-0.5 text-[10px] text-white/40">
                    {(s.ltv * 100).toFixed(0)}% · {s.amort_annees} ans · RCD{" "}
                    {s.rcd.toFixed(2)}
                  </p>
                  <dl className="mt-2 space-y-1">
                    {metricRows.map((r) => {
                      const val = r.pick(s);
                      const display =
                        val == null ? r.fallback || "—" : fmtMoney(val);
                      const tone =
                        r.colorEquite && val != null
                          ? val >= 0
                            ? "text-emerald-300"
                            : "text-rose-300"
                          : r.bold
                          ? "text-white"
                          : "text-white/80";
                      return (
                        <div
                          key={r.label}
                          className={`flex items-center justify-between gap-2 border-t pt-1 text-[11px] ${
                            r.keyRow
                              ? "border-brand-700"
                              : "border-brand-800/60"
                          }`}
                        >
                          <dt
                            className={
                              r.keyRow
                                ? "font-semibold text-white/70"
                                : "text-white/50"
                            }
                          >
                            {r.label}
                          </dt>
                          <dd
                            className={`font-mono tabular-nums ${tone} ${
                              r.bold ? "font-bold" : ""
                            }`}
                          >
                            {display}
                          </dd>
                        </div>
                      );
                    })}
                  </dl>
                </>
              ) : (
                <p className="mt-1 text-[11px] text-white/30">
                  Scénario non applicable.
                </p>
              )}
            </div>
          );
        })}
      </div>

      {/* Composition de la MDF : déplacée dans l'onglet ANALYSE
          (retour Phil 2026-09-02 — c'est de la saisie). */}

      {/* Projections long terme du mode prêteur B (retour Phil
          2026-09-02 : « tu pourrais aussi le rajouter dans la section
          prêteur B ») — le scénario refi gagnant continue de croître
          après le refi. */}
      {data.projection_preteur_b && data.projection_preteur_b.length > 0 ? (
        <div className="mt-4">
          <ProjectionSection
            proj={data.projection_preteur_b}
            titre="Après le refinancement — valeur vs solde du prêt (scénario gagnant)"
          />
        </div>
      ) : null}
    </SectionCard>
  );
}

// ─── Frais de démarrage ──────────────────────────────────────────

const FRAIS_KEYS: Array<keyof FraisDemarrageBreakdown> = [
  "courtier_hypothecaire_1",
  "courtier_hypothecaire_2",
  "taxes_bienvenue",
  "evaluateur",
  "evaluateur_2",
  "inspection",
  "avocat",
  "notaire",
  "notaire_2",
  "rapport_efficacite",
  "frais_developpement",
  "frais_negociations",
  "frais_travaux",
  // Mai 2026 : nouveau poste, juste après "Travaux estimés" comme
  // demandé par Phil (ordre figé pour respecter sa lecture habituelle).
  "frais_dossier_preteur",
  "interets",
  // Poste PERMANENT (retour Phil 2026-09-02) : 0 sans balance de
  // vente, non finançable par défaut.
  "interets_balance_vente",
  // Réserve de détention (institution traditionnelle), 0 par défaut.
  "detention",
  "revenus_nets_pendant_projet"
];

function _fmtPctShort(frac: number): string {
  const asPct = Math.abs(frac) <= 1 ? frac * 100 : frac;
  const isInt = Math.abs(asPct - Math.round(asPct)) < 0.01;
  return isInt
    ? `${Math.round(asPct)} %`
    : `${asPct.toFixed(1).replace(".", ",")} %`;
}

function buildFraisLabels(
  mdfPctNumeric: number,
  tauxInteretPreteurB: number | null | undefined,
  opts?: { trad?: boolean; chantier?: boolean; nAnnees?: number }
): Array<[keyof FraisDemarrageBreakdown, string]> {
  const trad = !!opts?.trad;
  const nAnnees = opts?.nAnnees ?? 2;
  const inverseMdf = 1 - mdfPctNumeric;
  const tauxLbl =
    tauxInteretPreteurB != null && !Number.isNaN(tauxInteretPreteurB)
      ? _fmtPctShort(tauxInteretPreteurB)
      : "taux prêteur B";
  const interetsLabel = `Intérêts pendant projet (${_fmtPctShort(inverseMdf)} × prix × ${tauxLbl} × durée)`;
  const labels: Record<keyof FraisDemarrageBreakdown, string> = {
    courtier_hypothecaire_1: trad
      ? "Courtier hypothécaire (1 % × prêt retenu)"
      : opts?.chantier
      ? "Courtier hypothécaire (1 % × prêt prêteur B)"
      : "Courtier hypothécaire (1 % × prix d'achat)",
    courtier_hypothecaire_2: "Courtier hypothécaire 2 (1 % × financement APH)",
    taxes_bienvenue: "Taxes de bienvenue (Montréal, tiers progressifs)",
    evaluateur: "Évaluateur agréé",
    evaluateur_2: "Évaluateur agréé 2",
    inspection: "Inspection",
    avocat: "Avocat",
    notaire: "Notaire",
    notaire_2: "Notaire 2",
    rapport_efficacite: "Rapport d'efficacité énergétique",
    frais_developpement: "Frais de développement",
    frais_negociations: "Frais de négociations",
    frais_travaux: "Travaux estimés",
    frais_dossier_preteur: trad
      ? "Frais de dossier de l'institution (montant fixe)"
      : "Frais de dossier du prêteur",
    interets: interetsLabel,
    interets_balance_vente: `Intérêts balance de vente (montant × taux × ${nAnnees} an${
      nAnnees > 1 ? "s" : ""
    })`,
    detention: "Détention (réserve pour la période de détention)",
    revenus_nets_pendant_projet: "Revenus nets pendant projet (négatif)"
  };
  return FRAIS_KEYS.map((k) => [k, labels[k]] as [keyof FraisDemarrageBreakdown, string]);
}

const DEFAULT_FINANCABLES = [
  "rapport_efficacite",
  "frais_developpement",
  "frais_travaux"
];

function FraisDemarrageBreakdownPanel({
  data,
  overridesJson,
  financablesJson,
  mdfPct,
  prixAchat,
  mdfPreteurBDb,
  onPatchOverrides,
  onPatchFinancables,
  mode = "preteur_b",
  pretRetenu,
  programmeLabel,
  chantier,
  dureeProjet
}: {
  data: AnalysisResults;
  overridesJson?: string | null;
  financablesJson?: string | null;
  mdfPct?: number;
  prixAchat?: number;
  mdfPreteurBDb?: number | null;
  onPatchOverrides?: (json: string) => void;
  onPatchFinancables?: (json: string) => void;
  /** traditionnel : assise = prix − prêt retenu, finançable = payé
   *  par l'institution (0 cash), rien de coché par défaut. */
  mode?: "preteur_b" | "traditionnel";
  pretRetenu?: number;
  programmeLabel?: string;
  /** Chantier stratégies actif (courtier 1 sur le prêt). */
  chantier?: boolean;
  /** Durée du projet (prêteur B) — libellé des intérêts BV. */
  dureeProjet?: number | null;
}) {
  const estTrad = mode === "traditionnel";
  // Institution traditionnelle : la source des montants est la liste
  // du MOTEUR pour ce mode (courtier sur le prêt, dossier fixe,
  // intérêts BV provisionnés, détention) — miroir exact du calcul.
  const fraisTrad = estTrad ? data.traditionnel?.frais_demarrage : undefined;
  const frais: FraisDemarrageBreakdown | undefined =
    data.frais_demarrage && fraisTrad
      ? ({ ...data.frais_demarrage, ...fraisTrad } as FraisDemarrageBreakdown)
      : data.frais_demarrage;
  // Cashback (retour Phil 2026-09-04) : prix déclaré = prix + cashback.
  const cashback = Math.max(
    0,
    data.cashback?.montant ?? data.traditionnel?.cashback ?? 0
  );
  const nAnneesRefiPanel = estTrad
    ? data.traditionnel?.horizon ?? 5
    : dureeProjet ?? 2;
  const mdfPctFinal = mdfPct ?? data.mdf_preteur_b_pct ?? 25;
  const mdfPctNumeric =
    mdfPctFinal > 1 ? mdfPctFinal / 100 : mdfPctFinal;
  const prixFinal = prixAchat ?? data.prix_achat ?? 0;
  const prixBancaire = prixFinal + cashback;
  const mdfPctValue = estTrad
    ? Math.max(0, prixBancaire - (pretRetenu ?? 0))
    : data.mdf_pct_prix_achat ??
      data.mdf_25pct_prix_achat ??
      mdfPctNumeric * prixBancaire;
  // Total du MOTEUR pour ce mode (badge « recalcul requis »).
  const mdfTotalStored = estTrad
    ? data.traditionnel?.mdf_cash ?? null
    : mdfPreteurBDb ?? data.mdf_preteur_b ?? null;

  const overrides = useMemo<Record<string, number>>(() => {
    if (!overridesJson) return {};
    try {
      const j = JSON.parse(overridesJson);
      if (j && typeof j === "object") return j as Record<string, number>;
    } catch {
      /* ignore */
    }
    return {};
  }, [overridesJson]);

  const financables = useMemo<Set<string>>(() => {
    if (financablesJson) {
      try {
        const j = JSON.parse(financablesJson);
        if (Array.isArray(j)) return new Set(j.map((x) => String(x)));
      } catch {
        /* ignore */
      }
    }
    return new Set(estTrad ? [] : DEFAULT_FINANCABLES);
  }, [financablesJson, estTrad]);

  function toggleFinancable(key: string) {
    const next = new Set(financables);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onPatchFinancables?.(JSON.stringify(Array.from(next)));
  }

  function setOverride(key: string, val: number | null) {
    const next = { ...overrides };
    if (val == null || !Number.isFinite(val)) {
      delete next[key];
    } else {
      next[key] = val;
    }
    onPatchOverrides?.(JSON.stringify(next));
  }

  const fraisLabels = buildFraisLabels(
    mdfPctNumeric,
    data.taux_interet_preteur_b_projet,
    { trad: estTrad, chantier, nAnnees: nAnneesRefiPanel }
  );
  // Libellés des postes FIXES indexés par clé, pour résoudre rapidement
  // un label par défaut quand le registre n'impose rien.
  const fixedLabelByKey = useMemo<Record<string, string>>(() => {
    const m: Record<string, string> = {};
    for (const [k, lbl] of fraisLabels) m[k as string] = lbl;
    return m;
  }, [fraisLabels]);

  // Postes de frais de démarrage PERSONNALISÉS (ajoutés dans Paramètres →
  // Calculateur, présents dans `frais_demarrage.frais_custom`). Affichés
  // même à 0 $. Leur finançabilité = `financable_par_defaut` (gérée dans
  // Paramètres) — cohérent avec le calcul backend.
  type CustomFraisRow = {
    id?: string;
    label_fr?: string;
    montant?: number;
    financable?: boolean;
  };
  const customFrais: CustomFraisRow[] = Array.isArray(
    (frais as { frais_custom?: unknown } | null)?.frais_custom
  )
    ? ((frais as { frais_custom?: CustomFraisRow[] }).frais_custom ?? [])
    : [];
  const customById = useMemo<Record<string, CustomFraisRow>>(() => {
    const m: Record<string, CustomFraisRow> = {};
    for (const c of customFrais) {
      const cid = String(c.id || "");
      if (cid) m[cid] = c;
    }
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(customFrais)]);

  // Registre unifié des frais de démarrage : liste ORDONNÉE
  // [{key, label_fr, visible}] où `key` est soit une clé de poste FIXE,
  // soit l'`id` d'un poste PERSONNALISÉ. Injecté par le backend dans
  // `analysis_results_json`. Absent (anciens résultats non recalculés)
  // → fallback sur l'ordre historique (fixes puis perso).
  type RegistryEntry = { key?: string; label_fr?: string; visible?: boolean };
  const registry: RegistryEntry[] | null = Array.isArray(
    (data as { frais_registry?: unknown }).frais_registry
  )
    ? ((data as { frais_registry?: RegistryEntry[] }).frais_registry ?? null)
    : null;

  // Modèle d'UNE ligne de poste, indépendant de l'origine (fixe / perso).
  // `key` = clé fixe OU id perso (sert d'identifiant override/finançable).
  type FraisLineRow = {
    key: string;
    label: string;
    computed: number;
    isCustom: boolean;
  };

  // Liste ORDONNÉE unifiée des lignes à afficher / sommer. Pilotée par le
  // registre quand il existe (ordre + labels + visibilité), sinon ordre
  // historique (fixes via `fraisLabels`, puis perso via `customFrais`).
  const fraisRows: FraisLineRow[] = useMemo(() => {
    const rows: FraisLineRow[] = [];
    if (registry) {
      for (const entry of registry) {
        const key = String(entry?.key || "");
        if (!key) continue;
        if (entry?.visible === false) continue; // poste masqué → sauter
        if (Object.prototype.hasOwnProperty.call(fixedLabelByKey, key)) {
          // Poste FIXE : valeur déjà calculée dans `frais[key]`.
          rows.push({
            key,
            label: String(entry?.label_fr || fixedLabelByKey[key] || key),
            computed: frais ? Number((frais as Record<string, number>)[key] || 0) : 0,
            isCustom: false
          });
        } else if (customById[key]) {
          // Poste PERSONNALISÉ : item matché par id.
          const c = customById[key];
          rows.push({
            key,
            label: String(entry?.label_fr || c.label_fr || "Frais personnalisé"),
            computed: Number(c.montant || 0),
            isCustom: true
          });
        }
        // Clé inconnue (ni fixe ni perso) → ignorée silencieusement.
      }
    } else {
      // Fallback : comportement historique strictement préservé.
      for (const [k, label] of fraisLabels) {
        rows.push({
          key: k as string,
          label,
          computed: frais ? Number((frais as Record<string, number>)[k] || 0) : 0,
          isCustom: false
        });
      }
      for (const c of customFrais) {
        const cid = String(c.id || "");
        rows.push({
          key: cid,
          label: c.label_fr || "Frais personnalisé",
          computed: Number(c.montant || 0),
          isCustom: true
        });
      }
    }
    // Institution traditionnelle : les postes propres au chantier
    // prêteur B n'existent pas (2e courtier/évaluateur/notaire,
    // portage, revenus pendant projet) — retour Phil 2026-09-04.
    const exclusTrad = new Set([
      "courtier_hypothecaire_2",
      "evaluateur_2",
      "notaire_2",
      "interets",
      "revenus_nets_pendant_projet"
    ]);
    const out = estTrad ? rows.filter((r) => !exclusTrad.has(r.key)) : rows;
    // Postes PERMANENTS : toujours présents même si le registre des
    // frais (Paramètres) ne les connaît pas encore.
    const permanents = estTrad
      ? ["interets_balance_vente", "detention"]
      : ["interets_balance_vente"];
    for (const pk of permanents) {
      if (!out.some((r) => r.key === pk)) {
        out.push({
          key: pk,
          label: fixedLabelByKey[pk] || pk,
          computed: frais
            ? Number((frais as Record<string, number>)[pk] || 0)
            : 0,
          isCustom: false
        });
      }
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    estTrad,
    JSON.stringify(registry),
    JSON.stringify(fraisLabels),
    JSON.stringify(customFrais),
    fixedLabelByKey,
    customById,
    frais
  ]);

  // Métriques d'une ligne (valeur affichée, cash, prêt) calculées avec la
  // MÊME logique override + finançable que l'affichage historique. Un poste
  // fixe à 0 $ non-override n'est PAS affiché (garde le filtre d'avant) ;
  // les perso s'affichent même à 0.
  function lineMetrics(row: FraisLineRow) {
    const { key } = row;
    const overridden = key !== "" && overrides[key] != null;
    const displayVal = overridden ? Number(overrides[key]) : row.computed;
    const isFin = key !== "" && financables.has(key);
    // Prêteur B : un poste finançable coûte pct en cash ; institution
    // traditionnelle : financé = roulé dans le prêt, 0 cash.
    const cashForRow = isFin
      ? (estTrad ? 0 : displayVal * mdfPctNumeric)
      : displayVal;
    const pretForRow = isFin ? displayVal - cashForRow : null;
    // Poste fixe nul et non-override → masqué (comme avant). Perso et
    // postes permanents (intérêts BV, détention) : toujours affichés.
    const permanent =
      key === "interets_balance_vente" || (estTrad && key === "detention");
    const hidden = !row.isCustom && !overridden && !row.computed && !permanent;
    return { overridden, displayVal, isFin, cashForRow, pretForRow, hidden };
  }

  let subTotalCash = 0;
  let subTotalFinanced = 0;
  // Somme brute de tous les postes (colonne « Valeur ») : la valeur totale
  // des frais de démarrage si AUCUN n'était finançable. Affichage seulement.
  let subTotalValeur = 0;
  if (frais) {
    // Sous-totaux sur la liste ORDONNÉE unifiée (mêmes formules qu'avant :
    // override de montant + finançable par fiche). On somme TOUTES les
    // lignes du registre (postes masqués déjà exclus de `fraisRows`).
    for (const row of fraisRows) {
      const { displayVal, isFin } = lineMetrics(row);
      const v = Number(displayVal);
      if (!Number.isFinite(v)) continue;
      subTotalValeur += v;
      if (isFin) {
        subTotalCash += estTrad ? 0 : v * mdfPctNumeric;
        subTotalFinanced += estTrad ? v : v * (1 - mdfPctNumeric);
      } else {
        subTotalCash += v;
      }
    }
  }
  const bvDeduiteMdf = data.balance_vente?.montant ?? 0;
  const totalMdfLocal =
    Math.max(0, mdfPctValue - bvDeduiteMdf - cashback) + subTotalCash;

  if (!frais) return null;

  // Y a-t-il au moins un poste perso à afficher (pour le titre de section
  // « personnalisés » du mode fallback) ?
  const hasCustomRows = customFrais.length > 0;

  return (
    <section className="mt-4 rounded-xl border border-brand-800 bg-brand-950/40 p-4">
      <div className="flex items-center gap-2.5">
        <span className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-amber-500/15 text-amber-300">
          <Wallet className="h-4 w-4" />
        </span>
        <h4 className="text-sm font-bold text-white">
          {estTrad
            ? "Composition de la mise de fonds (institution traditionnelle)"
            : "Composition de la MDF avec prêteur B"}
        </h4>
      </div>
      <p className="mt-2 text-[10px] leading-relaxed text-white/50">
        {estTrad ? (
          <>
            Total à sortir en cash = prix
            {cashback > 0 ? " déclaré à l'institution" : " d'achat"} −
            prêt retenu{cashback > 0 ? " − cashback reçu au notaire" : ""}
            {" "}− balance de vente + frais payés cash. Rien n&apos;est
            finançable par défaut — coche un poste pour le faire
            financer par l&apos;institution (roulé dans le prêt, 0 cash).
            Courtier 1 sur le prêt, frais de dossier fixes, intérêts de
            la balance de vente provisionnés pour la détention, poste
            « Détention » à 0 par défaut. Clique un montant pour le
            modifier.
          </>
        ) : (
          <>
            Total à sortir en cash = {_fmtPctShort(mdfPctNumeric)} du
            prix{cashback > 0 ? " déclaré" : " d'achat"}
            {cashback > 0 ? " − cashback reçu au notaire" : ""} − balance
            de vente + frais non finançables +{" "}
            {_fmtPctShort(mdfPctNumeric)} des frais finançables. Coche un poste pour le rendre finançable par
            le prêteur B (défaut : rapport efficacité, frais
            développement, travaux). Clique un montant pour le
            modifier.
          </>
        )}
      </p>

      <div className="mt-3 overflow-hidden rounded-lg border border-brand-800">
        <table className="w-full border-collapse text-[11px]">
          <thead>
            <tr className="bg-brand-900 text-[9px] font-semibold uppercase tracking-wider text-white/40">
              <th className="px-3 py-2 text-left">Poste</th>
              <th className="px-3 py-2 text-right">Valeur</th>
              <th
                className="w-20 px-3 py-2 text-center"
                title={
                  estTrad
                    ? "Activé = ce poste est roulé dans le prêt de l'institution (0 cash)"
                    : "Activé = ce poste est financé par le prêteur B, tu ne paies que le pct en cash"
                }
              >
                Finançable
              </th>
              <th className="px-3 py-2 text-right">Cash à sortir</th>
              <th
                className="px-3 py-2 text-right"
                title={
                  estTrad
                    ? "Portion financée par l'institution (postes cochés) et balance de vente"
                    : "Portion de la valeur financée par le prêteur B (valeur − cash) pour les postes finançables"
                }
              >
                {estTrad ? "Financé par l'institution" : "Prêt prêteur B"}
              </th>
            </tr>
          </thead>
          <tbody>
            {/* Ligne de base : assise de la MDF, mise en évidence (ambre) */}
            <tr className="border-y border-amber-400/30 bg-amber-500/10">
              <td className="px-3 py-2 font-semibold text-amber-200" colSpan={2}>
                {estTrad ? (
                  <>
                    {cashback > 0 ? "Prix déclaré" : "Prix d'achat"} − prêt
                    retenu{programmeLabel ? ` (${programmeLabel})` : ""}
                    {prixBancaire > 0 ? (
                      <span className="ml-1 font-normal text-white/50">
                        ({fmtMoney(prixBancaire)} −{" "}
                        {fmtMoney(pretRetenu ?? 0)})
                      </span>
                    ) : null}
                  </>
                ) : (
                  <>
                    {_fmtPctShort(mdfPctNumeric)} du prix
                    {cashback > 0 ? " déclaré" : " d'achat"}
                    {prixBancaire > 0 ? (
                      <span className="ml-1 font-normal text-white/50">
                        ({_fmtPctShort(mdfPctNumeric)} ×{" "}
                        {fmtMoney(prixBancaire)})
                      </span>
                    ) : null}
                  </>
                )}
              </td>
              {/* Balance de vente : financée par le vendeur → dans la
                  colonne Finançable, en vert, à gauche du cash
                  (retour Phil 2026-09-02). */}
              <td className="px-3 py-2 text-right font-mono tabular-nums text-emerald-300">
                {bvDeduiteMdf > 0 || cashback > 0 ? (
                  <span className="flex flex-col items-end leading-tight">
                    {cashback > 0 ? (
                      <span title="Cashback reçu au notaire — réduit le cash, sans intérêt">
                        − {fmtMoney(cashback)}{" "}
                        <span className="text-[9px] text-emerald-300/70">
                          cashback
                        </span>
                      </span>
                    ) : null}
                    {bvDeduiteMdf > 0 ? (
                      <span title="Balance de vente — financée par le vendeur">
                        − {fmtMoney(bvDeduiteMdf)}{" "}
                        <span className="text-[9px] text-emerald-300/70">
                          BV
                        </span>
                      </span>
                    ) : null}
                  </span>
                ) : (
                  <span className="text-white/40">—</span>
                )}
              </td>
              <td className="px-3 py-2 text-right font-mono font-semibold tabular-nums text-amber-200">
                {fmtMoney(Math.max(0, mdfPctValue - bvDeduiteMdf - cashback))}
              </td>
              <td className="px-3 py-2 text-right font-mono tabular-nums text-emerald-300/70">
                {bvDeduiteMdf > 0 ? fmtMoney(bvDeduiteMdf) : "—"}
              </td>
            </tr>
            <tr>
              <td
                className="px-3 pt-2.5 pb-1 text-[9px] font-semibold uppercase tracking-wider text-white/40"
                colSpan={5}
              >
                Frais de démarrage
              </td>
            </tr>
            {fraisRows.map((row, idx) => {
              const {
                overridden,
                displayVal,
                isFin,
                cashForRow,
                pretForRow,
                hidden
              } = lineMetrics(row);
              if (hidden) return null;
              const { key, label, computed, isCustom } = row;
              // En mode fallback (registre absent), on garde le sous-titre
              // historique « Frais de démarrage personnalisés » juste avant
              // le premier poste perso. En mode registre, l'ordre est unifié
              // et piloté par le registre → pas de séparateur.
              const showCustomHeader =
                !registry &&
                isCustom &&
                hasCustomRows &&
                fraisRows.slice(0, idx).every((r) => !r.isCustom);
              return (
                <Fragment key={key || `frais-${idx}`}>
                  {showCustomHeader ? (
                    <tr>
                      <td
                        className="px-3 pt-2.5 pb-1 text-[9px] font-semibold uppercase tracking-wider text-white/40"
                        colSpan={5}
                      >
                        Frais de démarrage personnalisés
                      </td>
                    </tr>
                  ) : null}
                  <tr
                    className={`border-t border-brand-800/50 ${
                      idx % 2 === 1 ? "bg-white/[0.015]" : ""
                    }`}
                  >
                    <td className="px-3 py-1.5 pl-5 text-white/60">
                      {label}
                      {overridden ? (
                        <button
                          type="button"
                          onClick={() => key && setOverride(key, null)}
                          className="ml-1.5 rounded bg-amber-500/20 px-1.5 py-0 text-[9px] font-medium text-amber-200 hover:bg-amber-500/30"
                          title="Réinitialiser à la valeur calculée"
                        >
                          override · réinit
                        </button>
                      ) : null}
                    </td>
                    <td className="px-3 py-1.5 text-right">
                      <EditableMoney
                        value={displayVal}
                        computed={computed}
                        overridden={overridden}
                        onSave={(v) =>
                          key && setOverride(key, v === computed ? null : v)
                        }
                      />
                    </td>
                    <td className="px-3 py-1.5 text-center">
                      <FinancableToggle
                        checked={isFin}
                        onToggle={() => key && toggleFinancable(key)}
                        title={
                          isFin
                            ? estTrad
                              ? "Finançable — roulé dans le prêt de l'institution, 0 cash"
                              : `Finançable — payé seulement à ${_fmtPctShort(mdfPctNumeric)} en cash`
                            : "Non finançable — payé 100 % en cash"
                        }
                      />
                    </td>
                    <td
                      className={`px-3 py-1.5 text-right font-mono tabular-nums ${
                        isFin ? "text-emerald-300" : "text-white/80"
                      }`}
                    >
                      {fmtMoney(cashForRow)}
                    </td>
                    <td
                      className={`px-3 py-1.5 text-right font-mono tabular-nums ${
                        pretForRow != null ? "text-emerald-300" : "text-white/40"
                      }`}
                    >
                      {pretForRow != null ? fmtMoney(pretForRow) : "—"}
                    </td>
                  </tr>
                </Fragment>
              );
            })}
          </tbody>
          <tfoot>
            {/* Les 3 lignes de total, démarquées et color-codées. */}
            <tr className="border-t border-amber-400/40 bg-amber-500/[0.07]">
              <td className="px-3 py-2 font-semibold text-amber-200">
                Sous-total frais de démarrage (cash)
              </td>
              <td
                className="px-3 py-2 text-right font-mono font-semibold tabular-nums text-amber-200"
                title="Valeur brute totale des frais de démarrage (si aucun n'était finançable)"
              >
                {fmtMoney(subTotalValeur)}
              </td>
              <td aria-hidden="true" />
              <td className="px-3 py-2 text-right font-mono font-semibold tabular-nums text-amber-200">
                {fmtMoney(subTotalCash)}
              </td>
              <td
                className="px-3 py-2 text-right font-mono font-semibold tabular-nums text-emerald-300"
                title={
                  estTrad
                    ? "Total roulé dans le prêt de l'institution"
                    : "Total financé par le prêteur B (= « dont financé par prêteur B »)"
                }
              >
                {subTotalFinanced > 0.5 ? fmtMoney(subTotalFinanced) : "—"}
              </td>
            </tr>
            {subTotalFinanced > 0.5 ? (
              <tr className="bg-emerald-500/[0.07]">
                <td
                  className="px-3 py-1.5 pl-5 text-[10px] font-semibold text-emerald-300"
                  colSpan={4}
                >
                  {estTrad
                    ? "dont financé par l'institution"
                    : "dont financé par prêteur B"}
                </td>
                <td className="px-3 py-1.5 text-right font-mono text-[10px] font-semibold tabular-nums text-emerald-300">
                  +{fmtMoney(subTotalFinanced)}
                </td>
              </tr>
            ) : null}
            <tr className="border-t-2 border-accent-500/50 bg-accent-500/10">
              <td className="px-3 py-2.5 font-bold text-white" colSpan={3}>
                {estTrad
                  ? "Total — mise de fonds (cash à l'achat)"
                  : "Total — MDF avec prêteur B"}
                {mdfTotalStored != null &&
                Math.abs((mdfTotalStored || 0) - totalMdfLocal) > 1 ? (
                  <span className="ml-2 rounded bg-amber-500/30 px-1.5 py-0 text-[9px] font-normal text-amber-100">
                    recalcul requis
                  </span>
                ) : null}
              </td>
              <td className="px-3 py-2.5 text-right font-mono text-sm font-bold tabular-nums text-accent-500">
                {fmtMoney(totalMdfLocal)}
              </td>
              <td aria-hidden="true" />
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  );
}

/**
 * Toggle custom « finançable » pour les postes de frais — remplace la
 * checkbox HTML brute par un switch propre et lisible (accent emerald
 * quand actif = financé par le prêteur B). Purement visuel : déclenche
 * `onToggle` exactement comme la checkbox d'origine.
 */
function FinancableToggle({
  checked,
  onToggle,
  title
}: {
  checked: boolean;
  onToggle: () => void;
  title?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={onToggle}
      title={title}
      className={`relative inline-flex h-4 w-7 flex-shrink-0 items-center rounded-full border transition-colors ${
        checked
          ? "border-emerald-400/60 bg-emerald-500/80"
          : "border-brand-600 bg-brand-800"
      }`}
    >
      <span
        className={`inline-block h-3 w-3 transform rounded-full bg-white shadow-sm transition-transform ${
          checked ? "translate-x-3.5" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

function EditableMoney({
  value,
  computed,
  overridden,
  onSave
}: {
  value: number;
  computed: number;
  overridden: boolean;
  onSave: (v: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(String(Math.round(value)));
  useEffect(() => {
    if (!editing) setDraft(String(Math.round(value)));
  }, [value, editing]);

  if (editing) {
    return (
      <input
        autoFocus
        type="number"
        value={draft}
        step="1"
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          setEditing(false);
          const n = Number(draft);
          if (Number.isFinite(n)) onSave(n);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            const n = Number(draft);
            if (Number.isFinite(n)) onSave(n);
            setEditing(false);
          } else if (e.key === "Escape") {
            setEditing(false);
            setDraft(String(Math.round(computed)));
          }
        }}
        className="w-28 rounded border border-amber-400/40 bg-brand-950 px-1 py-0.5 text-right font-mono text-[11px] text-white focus:border-accent-500 focus:outline-none"
      />
    );
  }
  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      title={
        overridden
          ? `Manuel (calcul auto : ${fmtMoney(computed)}). Clique pour modifier.`
          : "Clique pour overrider"
      }
      className={`font-mono tabular-nums hover:underline ${
        overridden ? "text-amber-200 font-semibold" : "text-white/80"
      }`}
    >
      {fmtMoney(value)}
    </button>
  );
}

function ResultRow({
  label,
  cols,
  pick,
  bold,
  fallback,
  colorEquite,
  keyRow,
  winnerIndex = -1
}: {
  label: string;
  cols: Array<[string, ScenarioResult | null]>;
  pick: (s: ScenarioResult) => number | null | undefined;
  bold?: boolean;
  fallback?: string;
  colorEquite?: boolean;
  keyRow?: boolean;
  winnerIndex?: number;
}) {
  const rowCls = keyRow
    ? "border-t border-brand-700 bg-white/[0.025]"
    : "border-t border-brand-800/50";
  return (
    <tr className={rowCls}>
      <td
        className={`bg-inherit px-2.5 py-2 ${
          keyRow ? "font-semibold text-white/80" : "text-white/60"
        }`}
      >
        {label}
      </td>
      {cols.map(([k, s], i) => {
        const isWinner = i === winnerIndex;
        const winnerBg = isWinner ? "bg-emerald-500/[0.07]" : "";
        if (!s) return (
          <td
            key={k}
            className={`px-2.5 py-2 text-right text-white/30 ${winnerBg}`}
          >
            —
          </td>
        );
        const val = pick(s);
        if (val == null) return (
          <td
            key={k}
            className={`px-2.5 py-2 text-right text-white/30 ${winnerBg}`}
          >
            {fallback || "—"}
          </td>
        );
        const txt = fmtMoney(val);
        // Couleur de la valeur. Les lignes color-codées (cashflow / équité)
        // gardent leur signal emerald/rose même dans la colonne gagnante —
        // on ne teinte en emerald « gagnant » que les lignes neutres.
        const tone = colorEquite
          ? val >= 0
            ? "text-emerald-300"
            : "text-rose-300"
          : isWinner
            ? "text-emerald-200"
            : bold
              ? "text-white"
              : "text-white/80";
        return (
          <td
            key={k}
            className={`px-2.5 py-2 text-right font-mono tabular-nums ${tone} ${
              bold ? "font-bold" : ""
            } ${winnerBg}`}
          >
            {txt}
          </td>
        );
      })}
    </tr>
  );
}

// ─── Détail granulaire des calculs (style Excel) ───────────────

function CalculationDetailsSection({
  resultsJson,
  overridesJson,
  lead
}: {
  resultsJson: string;
  overridesJson?: string | null;
  lead: LeadDetail;
}) {
  // Détails ouverts par défaut : la section occupe désormais son propre
  // onglet. Le repli reste disponible pour alléger la lecture.
  const [open, setOpen] = useState(true);

  const data = useMemo<AnalysisResults | null>(() => {
    try {
      return JSON.parse(resultsJson) as AnalysisResults;
    } catch {
      return null;
    }
  }, [resultsJson]);

  const overrides = useMemo<Record<string, number>>(() => {
    if (!overridesJson) return {};
    try {
      const j = JSON.parse(overridesJson);
      if (j && typeof j === "object") return j as Record<string, number>;
    } catch {
      /* ignore */
    }
    return {};
  }, [overridesJson]);

  if (!data) return null;

  return (
    <SectionCard
      icon={ListChecks}
      title="Détails des calculs"
      tone="neutral"
      subtitle="Reproduit la granularité du fichier Excel d'origine. Toutes les valeurs sont issues du dernier calcul persisté."
      action={
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="inline-flex items-center gap-1 rounded-md border border-white/10 bg-white/[0.03] px-2 py-1 text-[11px] text-white/60 hover:bg-white/10 hover:text-white"
        >
          {open ? "Replier" : "Déplier"}
        </button>
      }
    >
      {open ? (
        <div className="text-[11px] text-white/80">
          <HypothesesSubsection lead={lead} data={data} />
          <TypologieSubsection data={data} />
          <FraisDemarrageDetailSubsection
            data={data}
            overrides={overrides}
            mdfPctFinal={lead.mdf_preteur_b_pct ?? data.mdf_preteur_b_pct ?? 25}
            prixAchat={lead.asking_price ?? data.prix_achat ?? 0}
          />
          <ScenariosDetailSubsection data={data} />
          <BestRefiSubsection data={data} />
          <StrategieDetailSubsection data={data} lead={lead} />
        </div>
      ) : null}
    </SectionCard>
  );
}

/** Alias historique — délègue au formatage monétaire unifié. */
function _fmtMoneyDetail(n: number | null | undefined): string {
  return fmtMoney(n);
}

function _fmtPctDetail(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const asPct = Math.abs(n) <= 1 ? n * 100 : n;
  return `${asPct.toFixed(2).replace(".", ",")} %`;
}

function _fmtIntDetail(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.round(n).toString();
  return abs.replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

function _fmtBoolDetail(b: boolean | null | undefined): string {
  if (b == null) return "—";
  return b ? "Oui" : "Non";
}

function HypothesesSubsection({
  lead,
  data
}: {
  lead: LeadDetail;
  data: AnalysisResults;
}) {
  const prixAchat = lead.asking_price ?? data.prix_achat ?? null;
  const nbLog = lead.nb_logements;
  const coutParLogement =
    prixAchat != null && nbLog != null && nbLog > 0
      ? prixAchat / nbLog
      : null;

  const rows: Array<[string, string]> = [
    ["Prix d'achat", _fmtMoneyDetail(prixAchat)],
    ["Nombre de logements", _fmtIntDetail(nbLog)],
    ["Coût par logement", _fmtMoneyDetail(coutParLogement)],
    ["Durée du projet (années)", _fmtIntDetail(lead.duree_projet_annees)],
    ["TGA (taux global d'actualisation)", _fmtPctDetail(lead.tga_pct)],
    ["Taux d'intérêt achat", _fmtPctDetail(lead.taux_interet_achat_pct)],
    ["Taux d'intérêt refi", _fmtPctDetail(lead.taux_interet_refi_pct)],
    ["MDF prêteur B (%)", _fmtPctDetail(lead.mdf_preteur_b_pct ?? data.mdf_preteur_b_pct)],
    [
      "Taux d'intérêt prêteur B (chantier)",
      _fmtPctDetail(data.taux_interet_preteur_b_projet)
    ],
    ["Taux d'inoccupation", _fmtPctDetail(data.taux_inoccupation_pct)],
    ["Réduction énergie (refi)", _fmtPctDetail(lead.reduction_energie_pct)],
    ["WiFi ajouté (refi)", _fmtBoolDetail(lead.ajout_wifi)],
    ["Nb thermopompes ajoutées (APH)", _fmtIntDetail(lead.nb_thermopompes_ajoutees)],
    ["Nb logements ajoutés", _fmtIntDetail(lead.nb_logements_ajoutes)]
  ];
  return (
    <div className="mt-4">
      <h4 className="text-[10px] font-semibold uppercase tracking-wider text-accent-500">
        A · Hypothèses paramétrables
      </h4>
      <table className="mt-2 w-full">
        <tbody>
          {rows.map(([label, val]) => (
            <tr key={label} className="border-t border-brand-800/60">
              <td className="px-2 py-1 text-white/60">{label}</td>
              <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
                {val}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TypologieSubsection({ data }: { data: AnalysisResults }) {
  const t = data.typology;
  const rows: Array<[string, string]> = [
    ["H13 — loyer pondéré ($/mois)", _fmtMoneyDetail(t.h13_loyer_pondere)],
    ["Nb logements abordables", _fmtIntDetail(t.nb_abordables)],
    ["Nb logements PDM (programme de modulation)", _fmtIntDetail(t.nb_pdm)],
    ["Nouveau loyer moyen PDM ($/mois)", _fmtMoneyDetail(t.nouveau_loyer_moyen_pdm)]
  ];
  return (
    <div className="mt-4">
      <h4 className="text-[10px] font-semibold uppercase tracking-wider text-accent-500">
        B · Typologie agrégée
      </h4>
      <table className="mt-2 w-full">
        <tbody>
          {rows.map(([label, val]) => (
            <tr key={label} className="border-t border-brand-800/60">
              <td className="px-2 py-1 text-white/60">{label}</td>
              <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
                {val}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FraisDemarrageDetailSubsection({
  data,
  overrides,
  mdfPctFinal,
  prixAchat
}: {
  data: AnalysisResults;
  overrides: Record<string, number>;
  mdfPctFinal: number;
  prixAchat: number;
}) {
  const frais = data.frais_demarrage;
  const financables = new Set(data.frais_demarrage_financables ?? []);
  const mdfPctNumeric = mdfPctFinal > 1 ? mdfPctFinal / 100 : mdfPctFinal;
  const mdfFromPrice = mdfPctNumeric * prixAchat;
  const mdfTotal = data.mdf_preteur_b ?? null;

  if (!frais) {
    return (
      <div className="mt-4">
        <h4 className="text-[10px] font-semibold uppercase tracking-wider text-accent-500">
          C · Frais de démarrage
        </h4>
        <p className="mt-2 text-white/40">
          Détail indisponible dans cette version d&apos;analyse.
        </p>
      </div>
    );
  }

  const fraisLabels = buildFraisLabels(
    mdfPctNumeric,
    data.taux_interet_preteur_b_projet
  );

  return (
    <div className="mt-4">
      <h4 className="text-[10px] font-semibold uppercase tracking-wider text-accent-500">
        C · Frais de démarrage (détail par poste)
      </h4>
      <table className="mt-2 w-full">
        <thead>
          <tr className="text-white/40">
            <th className="px-2 py-1 text-left font-normal">Poste</th>
            <th className="px-2 py-1 text-right font-normal">Valeur</th>
            <th className="px-2 py-1 text-center font-normal">Override</th>
            <th className="px-2 py-1 text-center font-normal">Finançable</th>
          </tr>
        </thead>
        <tbody>
          {fraisLabels.map(([key, label]) => {
            const v = frais[key];
            const isOverridden = Object.prototype.hasOwnProperty.call(
              overrides,
              key
            );
            const isFinancable = financables.has(key);
            return (
              <tr key={key} className="border-t border-brand-800/60">
                <td className="px-2 py-1 text-white/60">{label}</td>
                <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
                  {_fmtMoneyDetail(v)}
                </td>
                <td className="px-2 py-1 text-center text-white/40">
                  {isOverridden ? "manuel" : "calculé"}
                </td>
                <td className="px-2 py-1 text-center text-white/40">
                  {isFinancable ? "oui" : "—"}
                </td>
              </tr>
            );
          })}
          <tr className="border-t-2 border-brand-700">
            <td className="px-2 py-1 font-semibold text-white">
              Total frais de démarrage
            </td>
            <td className="px-2 py-1 text-right font-mono font-bold tabular-nums text-white">
              {_fmtMoneyDetail(data.frais_demarrage_total)}
            </td>
            <td colSpan={2} />
          </tr>
        </tbody>
      </table>

      <div className="mt-3 rounded-lg border border-amber-400/30 bg-amber-500/5 p-3">
        <p className="text-[10px] uppercase tracking-wider text-amber-300">
          MDF prêteur B — composition
        </p>
        <table className="mt-1 w-full">
          <tbody>
            <tr className="border-t border-brand-800/60">
              <td className="px-2 py-1 text-white/60">
                {_fmtPctDetail(mdfPctNumeric)} × prix d&apos;achat
                ({_fmtMoneyDetail(prixAchat)})
              </td>
              <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
                {_fmtMoneyDetail(mdfFromPrice)}
              </td>
            </tr>
            <tr className="border-t border-brand-800/60">
              <td className="px-2 py-1 text-white/60">
                + Frais de démarrage total (cash après finançables)
              </td>
              <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
                {_fmtMoneyDetail(data.frais_demarrage_total)}
              </td>
            </tr>
            <tr className="border-t-2 border-brand-700">
              <td className="px-2 py-1 font-semibold text-amber-200">
                = MDF prêteur B total (cash)
              </td>
              <td className="px-2 py-1 text-right font-mono font-bold tabular-nums text-amber-200">
                {_fmtMoneyDetail(mdfTotal)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

const DEPENSES_LABELS: Array<[keyof DepensesBreakdown, string]> = [
  ["inoccupation", "Inoccupation (% × revenus)"],
  ["taxes_municipales", "Taxes municipales"],
  ["taxes_scolaires", "Taxes scolaires"],
  ["assurances", "Assurances"],
  ["energie", "Énergie"],
  ["concierge", "Concierge"],
  ["entretien", "Entretien"],
  ["gestion", "Gestion"],
  ["wifi", "WiFi"],
  ["thermopompes", "Thermopompes (APH)"],
  ["autres_normalisations", "Autres normalisations (% × revenus)"],
  ["autres", "Autres dépenses"]
];

function ScenariosDetailSubsection({ data }: { data: AnalysisResults }) {
  const scenarios: Array<[string, ScenarioResult | null]> = [
    ["Achat", data.scenarios.achat],
    ["SCHL standard", data.scenarios.refi_schl],
    ["APH 50 pts (Efficacité)", data.scenarios.refi_aph_50],
    ["APH 100 pts (Abord + Eff.)", data.scenarios.refi_aph_100]
  ];

  return (
    <div className="mt-4">
      <h4 className="text-[10px] font-semibold uppercase tracking-wider text-accent-500">
        D · Détail par scénario
      </h4>
      <div className="mt-2 space-y-4">
        {scenarios.map(([label, s]) => (
          <ScenarioDetailCard key={label} label={label} scenario={s} />
        ))}
      </div>
    </div>
  );
}

function ScenarioDetailCard({
  label,
  scenario
}: {
  label: string;
  scenario: ScenarioResult | null;
}) {
  if (!scenario) {
    return (
      <div className="rounded-lg border border-brand-800 bg-brand-950 p-3">
        <p className="text-[11px] font-semibold text-white/80">{label}</p>
        <p className="mt-1 text-[10px] text-white/40">
          Scénario non applicable (ex. pas d&apos;abordabilité).
        </p>
      </div>
    );
  }
  const dep = scenario.depenses;
  return (
    <div className="rounded-lg border border-brand-800 bg-brand-950 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-[11px] font-semibold text-white">{label}</p>
        <p className="text-[10px] text-white/40">
          LTV {(scenario.ltv * 100).toFixed(0)} % · amort.{" "}
          {scenario.amort_annees} ans · RCD {scenario.rcd.toFixed(2)}
        </p>
      </div>

      <table className="mt-2 w-full">
        <tbody>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">Nombre de logements</td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
              {_fmtIntDetail(scenario.nb_log)}
            </td>
          </tr>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">Loyer moyen ($/mois)</td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
              {_fmtMoneyDetail(scenario.loyer_mois)}
            </td>
          </tr>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">Revenus totaux ($/an)</td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
              {_fmtMoneyDetail(scenario.revenus_totaux)}
            </td>
          </tr>
        </tbody>
      </table>

      <p className="mt-3 text-[10px] uppercase tracking-wider text-white/40">
        Dépenses
      </p>
      <table className="mt-1 w-full">
        <tbody>
          {DEPENSES_LABELS.map(([key, lbl]) => (
            <tr key={key} className="border-t border-brand-800/60">
              <td className="px-2 py-1 text-white/60">{lbl}</td>
              <td className="px-2 py-1 text-right font-mono tabular-nums text-white/80">
                {dep ? _fmtMoneyDetail(dep[key]) : "—"}
              </td>
            </tr>
          ))}
          <tr className="border-t-2 border-brand-700">
            <td className="px-2 py-1 font-semibold text-white">
              Total dépenses
            </td>
            <td className="px-2 py-1 text-right font-mono font-bold tabular-nums text-white">
              {_fmtMoneyDetail(scenario.depenses_total)}
            </td>
          </tr>
        </tbody>
      </table>

      <p className="mt-3 text-[10px] uppercase tracking-wider text-white/40">
        Valeurs et financement
      </p>
      <table className="mt-1 w-full">
        <tbody>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">
              Revenus nets (revenus − dépenses)
            </td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-white/90">
              {_fmtMoneyDetail(scenario.revenus_net)}
            </td>
          </tr>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">Valeur éco RCD</td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-white/80">
              {_fmtMoneyDetail(scenario.valeur_eco_rcd)}
            </td>
          </tr>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">Valeur éco TGA</td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-white/80">
              {_fmtMoneyDetail(scenario.valeur_eco_tga)}
            </td>
          </tr>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">Valeur marchande</td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-white/80">
              {_fmtMoneyDetail(scenario.valeur_marchande)}
            </td>
          </tr>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 font-semibold text-white">
              Valeur retenue
            </td>
            <td className="px-2 py-1 text-right font-mono font-bold tabular-nums text-white">
              {_fmtMoneyDetail(scenario.valeur_retenue)}
            </td>
          </tr>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 font-semibold text-white">
              Financement (prêt accordé)
            </td>
            <td className="px-2 py-1 text-right font-mono font-bold tabular-nums text-white">
              {_fmtMoneyDetail(scenario.financement)}
            </td>
          </tr>
          {scenario.mdf_necessaire != null ? (
            <tr className="border-t border-brand-800/60">
              <td className="px-2 py-1 text-white/60">MDF nécessaire</td>
              <td className="px-2 py-1 text-right font-mono tabular-nums text-amber-200">
                {_fmtMoneyDetail(scenario.mdf_necessaire)}
              </td>
            </tr>
          ) : null}
          {scenario.equite_a_la_fin != null ? (
            <tr className="border-t border-brand-800/60">
              <td className="px-2 py-1 text-white/60">Équité à la fin</td>
              <td
                className={`px-2 py-1 text-right font-mono tabular-nums ${scenario.equite_a_la_fin >= 0 ? "text-emerald-300" : "text-rose-300"}`}
              >
                {_fmtMoneyDetail(scenario.equite_a_la_fin)}
              </td>
            </tr>
          ) : null}
          {scenario.cashflow_annuel != null ? (
            <tr className="border-t border-brand-800/60">
              <td className="px-2 py-1 text-white/60">Cashflow annuel</td>
              <td
                className={`px-2 py-1 text-right font-mono tabular-nums ${scenario.cashflow_annuel >= 0 ? "text-emerald-300" : "text-rose-300"}`}
              >
                {_fmtMoneyDetail(scenario.cashflow_annuel)}
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}

function BestRefiSubsection({ data }: { data: AnalysisResults }) {
  return (
    <div className="mt-4">
      <h4 className="text-[10px] font-semibold uppercase tracking-wider text-accent-500">
        E · Best refi (scénario retenu)
      </h4>
      <table className="mt-2 w-full">
        <tbody>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 text-white/60">Programme retenu</td>
            <td className="px-2 py-1 text-right font-mono tabular-nums text-emerald-300">
              {data.best_refi.program}
            </td>
          </tr>
          <tr className="border-t border-brand-800/60">
            <td className="px-2 py-1 font-semibold text-white">
              Montant (équité finale)
            </td>
            <td className="px-2 py-1 text-right font-mono font-bold tabular-nums text-emerald-300">
              {_fmtMoneyDetail(data.best_refi.amount)}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function _ligne2(label: string, valeur: string, bold = false) {
  return (
    <tr className="border-t border-brand-800/60" key={label}>
      <td
        className={`px-2 py-1 ${bold ? "font-semibold text-white" : "text-white/60"}`}
      >
        {label}
      </td>
      <td
        className={`px-2 py-1 text-right font-mono tabular-nums ${
          bold ? "font-bold text-white" : "text-white/80"
        }`}
      >
        {valeur}
      </td>
    </tr>
  );
}

/** F · Stratégie d'acquisition — TOUT le détail du chantier :
 *  croissances, unités, balance de vente, MDF, colonnes achat/refi du
 *  mode traditionnel et projections (retour Phil 2026-09-02). */
function StrategieDetailSubsection({
  data,
  lead
}: {
  data: AnalysisResults;
  lead: LeadDetail;
}) {
  const trad = data.traditionnel;
  const unites = (() => {
    try {
      const j = lead.unites_json ? JSON.parse(lead.unites_json) : null;
      return Array.isArray(j) ? (j as Array<Record<string, unknown>>) : [];
    } catch {
      return [];
    }
  })();
  const chantier = Boolean(
    trad || data.projection_preteur_b || unites.length > 0
  );
  if (!chantier) return null;
  const cl = (lead.tri_croissance_loyers ?? 0.03) * 100;
  const cd = (lead.tri_croissance_depenses ?? 0.03) * 100;
  const bv = data.balance_vente?.montant ?? 0;
  // Nombre d'années jusqu'au refi : durée du projet en prêteur B,
  // horizon de détention en traditionnel.
  const nAnneesRefi = trad
    ? trad.horizon
    : lead.duree_projet_annees ?? 2;
  const facteurRefi = Math.pow(1 + cl / 100, nAnneesRefi);
  // Dépenses poste par poste, par scénario (les deux modes).
  const depensesParScenario: Array<
    [string, Partial<Record<keyof DepensesBreakdown, number>> | undefined]
  > = trad
    ? [
        [
          `Achat (${trad.labels[trad.programme_retenu]})`,
          trad.achat[trad.programme_retenu]?.depenses
        ],
        ...Object.entries(trad.refi).map(
          ([k, s2]) =>
            [`Refi ${trad.labels[k] || k}`, s2?.depenses] as [
              string,
              Partial<Record<keyof DepensesBreakdown, number>> | undefined
            ]
        )
      ]
    : [
        ["Achat", data.scenarios.achat?.depenses],
        ["Refi SCHL", data.scenarios.refi_schl?.depenses],
        ["Refi APH 50", data.scenarios.refi_aph_50?.depenses],
        ["Refi APH 100", data.scenarios.refi_aph_100?.depenses]
      ];
  return (
    <div className="mt-4">
      <h4 className="text-[10px] font-semibold uppercase tracking-wider text-accent-500">
        F · Stratégie d&apos;acquisition (chantier)
      </h4>
      <table className="mt-2 w-full text-[11px]">
        <tbody>
          {_ligne2(
            "Stratégie",
            (lead.strategie_acquisition ?? "preteur_b") === "preteur_b"
              ? "Prêteur B + optimisation + refi"
              : "Institution traditionnelle"
          )}
          {trad
            ? _ligne2(
                "Programme d'achat retenu",
                trad.labels[trad.programme_retenu] || trad.programme_retenu
              )
            : null}
          {(data.cashback?.montant ?? 0) > 0
            ? _ligne2(
                "Cashback (prix déclaré à l'institution)",
                `${_fmtMoneyDetail(data.cashback?.montant ?? 0)} reçu au notaire · prix déclaré ${_fmtMoneyDetail(data.cashback?.prix_bancaire ?? 0)} → prêt calculé sur ce prix, cash réduit d'autant`
              )
            : null}
          {trad
            ? _ligne2("Horizon de détention", `${trad.horizon} ans`)
            : null}
          {_ligne2("Croissance revenus organique", `${cl.toFixed(1)} % / an`)}
          {_ligne2("Croissance dépenses", `${cd.toFixed(1)} % / an`)}
          {_ligne2(
            "Référence de refinancement",
            lead.refi_retenu || "automatique (meilleur)"
          )}
          {bv > 0
            ? _ligne2(
                "Balance de vente (déduite de la MDF)",
                `${_fmtMoneyDetail(bv)} · taux ${(data.balance_vente?.taux_pct ?? 0).toFixed(2)} % · intérêts provisionnés ${_fmtMoneyDetail((trad ? trad.frais_demarrage?.interets_balance_vente : data.frais_demarrage?.interets_balance_vente) ?? 0)} (${trad ? `${trad.interets_bv_annuels.toFixed(0)} $/an × ${trad.horizon} ans` : "montant × taux × durée"})`
              )
            : _ligne2("Balance de vente", "aucune")}
          {trad
            ? _ligne2(
                "MDF cash (programme retenu)",
                _fmtMoneyDetail(trad.mdf_cash),
                true
              )
            : null}
          {trad
            ? _ligne2(
                `Solde du prêt d'achat à l'an ${trad.horizon}`,
                _fmtMoneyDetail(trad.solde_retenu_an_h)
              )
            : null}
        </tbody>
      </table>

      {unites.length > 0 ? (
        <>
          <p className="mt-3 text-[10px] font-semibold uppercase tracking-wider text-white/40">
            Unités ({unites.length}) — cochée = loyer cible au refi,
            décochée = loyer actuel × (1 + {cl.toFixed(1)} %)^
            {nAnneesRefi} (croissance organique jusqu&apos;au refi)
          </p>
          <table className="mt-1 w-full text-[11px]">
            <tbody>
              {unites.map((u, i) => {
                const actuel = Number(u.loyer_actuel) || 0;
                const cible = Number(u.loyer_cible) || 0;
                const nonOpt = u.optimiser === false;
                const resultat = nonOpt
                  ? actuel * facteurRefi
                  : cible || actuel;
                return (
                  <tr key={i} className="border-t border-brand-800/60">
                    <td className="px-2 py-1 text-white/60">
                      {String(u.typo || "—")} · #{i + 1}
                      {nonOpt ? " (non optimisée)" : " (optimisée)"}
                    </td>
                    <td className="px-2 py-1 text-right font-mono tabular-nums text-white/80">
                      {nonOpt
                        ? `${_fmtMoneyDetail(actuel)} × ${facteurRefi.toFixed(4)} = `
                        : `${_fmtMoneyDetail(actuel)} → cible `}
                      <b className="text-white">
                        {_fmtMoneyDetail(resultat)}
                      </b>
                      {" /mois au refi"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="mt-1 px-2 text-[10px] text-white/40">
            Somme au refi :{" "}
            {_fmtMoneyDetail(
              unites.reduce((s2, u) => {
                const actuel = Number(u.loyer_actuel) || 0;
                const cible = Number(u.loyer_cible) || 0;
                return (
                  s2 +
                  (u.optimiser === false
                    ? actuel * facteurRefi
                    : cible || actuel)
                );
              }, 0)
            )}{" "}
            /mois × 12 (avant unités ajoutées).
          </p>
        </>
      ) : null}

      {/* Dépenses poste par poste, par scénario — « tous les
          détails » (Phil 2026-09-02). */}
      {depensesParScenario.length > 0 ? (
        <>
          <p className="mt-3 text-[10px] font-semibold uppercase tracking-wider text-white/40">
            Dépenses détaillées par scénario ($/an)
          </p>
          <div className="mt-1 overflow-x-auto">
            <table className="w-full text-[10px]">
              <thead>
                <tr className="text-[9px] uppercase tracking-wider text-white/40">
                  <td className="px-2 py-1">Poste</td>
                  {depensesParScenario.map(([lbl]) => (
                    <td key={lbl} className="px-2 py-1 text-right">
                      {lbl}
                    </td>
                  ))}
                </tr>
              </thead>
              <tbody>
                {DEPENSES_LABELS.map(([key, lbl]) => (
                  <tr key={key} className="border-t border-brand-800/60">
                    <td className="px-2 py-1 text-white/60">{lbl}</td>
                    {depensesParScenario.map(([slbl, deps]) => (
                      <td
                        key={slbl}
                        className="px-2 py-1 text-right font-mono tabular-nums text-white/80"
                      >
                        {_fmtMoneyDetail(Number(deps?.[key] ?? 0))}
                      </td>
                    ))}
                  </tr>
                ))}
                <tr className="border-t-2 border-brand-700 font-bold">
                  <td className="px-2 py-1 text-white">Total</td>
                  {depensesParScenario.map(([slbl, deps]) => (
                    <td
                      key={slbl}
                      className="px-2 py-1 text-right font-mono tabular-nums text-white"
                    >
                      {_fmtMoneyDetail(
                        DEPENSES_LABELS.reduce(
                          (s2, [k]) => s2 + Number(deps?.[k] ?? 0),
                          0
                        )
                      )}
                    </td>
                  ))}
                </tr>
              </tbody>
            </table>
          </div>
        </>
      ) : null}

      {trad ? (
        <>
          <p className="mt-3 text-[10px] font-semibold uppercase tracking-wider text-white/40">
            Achat — 4 programmes (loyers/dépenses actuels)
          </p>
          <table className="mt-1 w-full text-[11px]">
            <tbody>
              {Object.entries(trad.achat).map(([k, s]) =>
                s
                  ? _ligne2(
                      `${trad.labels[k] || k} — prêt / MDF / cashflow`,
                      `${_fmtMoneyDetail(s.financement)} · ${_fmtMoneyDetail(trad.mdf_par_programme[k] ?? 0)} · ${_fmtMoneyDetail(s.cashflow_annuel ?? 0)}/an`
                    )
                  : null
              )}
            </tbody>
          </table>
          <p className="mt-3 text-[10px] font-semibold uppercase tracking-wider text-white/40">
            Refinancement an {trad.horizon} — 4 programmes (revenus{" "}
            {_fmtMoneyDetail(
              Object.values(trad.refi)[0]?.revenus_totaux ?? 0
            )}
            /an projetés)
          </p>
          <table className="mt-1 w-full text-[11px]">
            <tbody>
              {Object.entries(trad.refi).map(([k, s]) =>
                s
                  ? _ligne2(
                      `${trad.labels[k] || k} — prêt max / argent dégagé`,
                      `${_fmtMoneyDetail(s.financement)} · ${_fmtMoneyDetail(s.equite_a_la_fin ?? 0)}`
                    )
                  : null
              )}
            </tbody>
          </table>
        </>
      ) : null}
    </div>
  );
}

// ─── Phase A3 : Panneau "Validation de l'extraction" ──────────────

const FIELD_LABELS: Record<string, string> = {
  asking_price: "Prix demandé",
  nb_logements: "Nombre de logements",
  revenus_bruts: "Revenus bruts",
  taxes_municipales: "Taxes municipales",
  taxes_scolaires: "Taxes scolaires",
  assurances: "Assurances",
  energie: "Énergie",
  evaluation_municipale: "Évaluation municipale",
  superficie_terrain: "Superficie terrain",
  superficie_batiment: "Superficie bâtiment",
  annee_construction: "Année construction"
};

function _fmtSourceValue(v: number | string | null | undefined): string {
  if (v == null || v === "") return "—";
  if (typeof v === "number") {
    const rounded = Math.round(v);
    const sign = rounded < 0 ? "-" : "";
    const abs = Math.abs(rounded).toString();
    const withSep = abs.replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    return `${sign}${withSep}`;
  }
  return String(v);
}

function ValidationPanel({
  warnings
}: {
  warnings: ValidationWarning[] | null;
}) {
  const list = warnings || [];

  if (list.length === 0) {
    return (
      <SectionCard
        icon={CheckCircle2}
        title="Validation de l'extraction"
        tone="emerald"
      >
        <div className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">
          <CheckCircle2 className="h-4 w-4 flex-shrink-0" />
          <span>Aucune anomalie détectée sur les champs extraits.</span>
        </div>
      </SectionCard>
    );
  }

  return (
    <SectionCard
      icon={AlertTriangle}
      title={`Validation de l'extraction (${list.length})`}
      tone="amber"
    >
      <ul className="space-y-2">
        {list.map((w, i) => {
          const sevCls =
            w.severity === "error"
              ? "border-rose-500/40 bg-rose-500/10 text-rose-300"
              : w.severity === "warning"
              ? "border-amber-500/40 bg-amber-500/10 text-amber-300"
              : "border-blue-500/40 bg-blue-500/10 text-blue-300";
          const Icon =
            w.severity === "error"
              ? Ban
              : w.severity === "warning"
              ? AlertTriangle
              : Info;
          const fieldLabel = FIELD_LABELS[w.field] || w.field;
          const hasSources =
            w.source_local != null ||
            w.source_gemini != null ||
            w.source_claude != null;
          return (
            <li
              key={i}
              className={`rounded-md border px-3 py-2 text-xs ${sevCls}`}
            >
              <div className="flex items-start gap-2">
                <Icon className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <div className="min-w-0 flex-1">
                  <p className="font-semibold">{fieldLabel}</p>
                  <p className="mt-0.5 text-white/80">{w.message}</p>
                  {hasSources ? (
                    <div className="mt-1.5 flex flex-wrap gap-3 text-[10px] text-white/60">
                      {w.source_local != null ? (
                        <span>
                          <span className="font-semibold text-white/70">
                            Local :
                          </span>{" "}
                          <span className="font-mono tabular-nums">
                            {_fmtSourceValue(w.source_local)}
                          </span>
                        </span>
                      ) : null}
                      {w.source_gemini != null ? (
                        <span>
                          <span className="font-semibold text-white/70">
                            Gemini :
                          </span>{" "}
                          <span className="font-mono tabular-nums">
                            {_fmtSourceValue(w.source_gemini)}
                          </span>
                        </span>
                      ) : null}
                      {w.source_claude != null ? (
                        <span>
                          <span className="font-semibold text-white/70">
                            Claude :
                          </span>{" "}
                          <span className="font-mono tabular-nums">
                            {_fmtSourceValue(w.source_claude)}
                          </span>
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </SectionCard>
  );
}

// ─── Badge "Extrait par" + ReExtract buttons ─────────────────────

const EXTRACTION_BADGE_CLS: Record<string, string> = {
  local: "border-slate-500/30 bg-slate-500/10 text-slate-300",
  "local + gemini": "border-blue-500/30 bg-blue-500/10 text-blue-300",
  gemini: "border-violet-500/30 bg-violet-500/10 text-violet-300",
  claude: "border-amber-500/30 bg-amber-500/10 text-amber-300",
  groq: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
  none: "border-rose-500/30 bg-rose-500/10 text-rose-300"
};

function extractionBadgeMeta(
  modelUsed: string | null | undefined
): { label: string; cls: string } {
  const m = (modelUsed || "").toLowerCase();
  if (m.startsWith("claude"))
    return { label: "Claude", cls: EXTRACTION_BADGE_CLS.claude };
  if (m.includes("llama") || m.includes("groq"))
    return { label: "Groq", cls: EXTRACTION_BADGE_CLS.groq };
  if (m.startsWith("local + gemini") || m === "local + gemini")
    return {
      label: "Local + Gemini",
      cls: EXTRACTION_BADGE_CLS["local + gemini"]
    };
  if (m.startsWith("gemini"))
    return { label: "Gemini", cls: EXTRACTION_BADGE_CLS.gemini };
  if (m === "local")
    return { label: "Parser local", cls: EXTRACTION_BADGE_CLS.local };
  return { label: "Aucune extraction", cls: EXTRACTION_BADGE_CLS.none };
}

function ExtractionBadgeInline({
  modelUsed
}: {
  modelUsed: string | null | undefined;
}) {
  const meta = extractionBadgeMeta(modelUsed);
  return (
    <span
      title={`Extrait par : ${modelUsed || "aucun modele"}`}
      className={`mt-1 inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${meta.cls}`}
    >
      <Sparkles className="h-3 w-3" />
      Extrait par : {meta.label}
    </span>
  );
}

function ReExtractButtons({
  id,
  hasSources,
  onSuccess
}: {
  id: number;
  hasSources: boolean;
  onSuccess: () => Promise<void>;
}) {
  const confirm = useConfirm();
  const [busyKind, setBusyKind] = useState<"groq" | "claude" | null>(null);
  const [toast, setToast] = useState<{
    text: string;
    kind: "ok" | "err";
  } | null>(null);
  const [claudeEnabled, setClaudeEnabled] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await authedFetch(
          "/api/v1/lead-analyses/check-claude-health"
        );
        if (!r.ok) {
          if (!cancelled) setClaudeEnabled(false);
          return;
        }
        const out = (await r.json()) as { enabled?: boolean };
        if (!cancelled) setClaudeEnabled(out.enabled === true);
      } catch {
        if (!cancelled) setClaudeEnabled(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!toast || toast.kind === "err") return;
    const t = setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(t);
  }, [toast]);

  async function runGroq() {
    if (!hasSources || busyKind) return;
    const ok = await confirm({
      title: "Ré-extraire avec Groq ?",
      description:
        "Cette opération est gratuite (Groq Llama 3.3 70B). Groq " +
        "relit les sources originales de la fiche (URLs, texte, PDF/" +
        "images OCR-isés) et remplit les champs vides. Les champs " +
        "déjà saisis ne sont pas écrasés (sauf adresse/ville). " +
        "Continuer ?",
      confirmLabel: "Lancer Groq"
    });
    if (!ok) return;
    setBusyKind("groq");
    setToast(null);
    try {
      const r = await authedFetch(
        `/api/v1/lead-analyses/${id}/re-extract-with-groq`,
        { method: "POST" }
      );
      if (!r.ok) {
        const detail = await r
          .json()
          .then((j: { detail?: string }) => j.detail || `HTTP ${r.status}`)
          .catch(() => `HTTP ${r.status}`);
        const text =
          r.status === 503
            ? `Extraction Groq indisponible : ${detail}`
            : `Ré-extraction Groq échouée (HTTP ${r.status}) : ${detail}`;
        setToast({ text, kind: "err" });
        return;
      }
      const out = (await r.json()) as {
        fields_patched: string[];
        model_used: string;
      };
      await onSuccess();
      const n = out.fields_patched?.length || 0;
      setToast({
        text:
          n > 0
            ? `Champs ré-extraits par Groq (${n}). Vérifie les modifications.`
            : "Groq n'a pas trouvé de nouveaux champs à remplir.",
        kind: "ok"
      });
    } catch (e) {
      setToast({
        text: `Ré-extraction Groq échouée : ${(e as Error).message}`,
        kind: "err"
      });
    } finally {
      setBusyKind(null);
    }
  }

  async function runClaude() {
    if (!hasSources || busyKind) return;
    const ok = await confirm({
      title: "Ré-extraire avec Claude ?",
      description:
        "Cette opération coûte environ 3 cents (Claude Sonnet 4.6, " +
        "multimodal). Claude relit les sources originales et remplit " +
        "les champs vides. Les champs déjà saisis ne sont pas écrasés " +
        "(sauf adresse/ville). Continuer ?",
      confirmLabel: "Lancer Claude (~3¢)"
    });
    if (!ok) return;
    setBusyKind("claude");
    setToast(null);
    try {
      const r = await authedFetch(
        `/api/v1/lead-analyses/${id}/re-extract-with-claude`,
        { method: "POST" }
      );
      if (!r.ok) {
        const detail = await r
          .json()
          .then((j: { detail?: string }) => j.detail || `HTTP ${r.status}`)
          .catch(() => `HTTP ${r.status}`);
        const text =
          r.status === 503
            ? `Extraction Claude indisponible : ${detail}`
            : `Ré-extraction Claude échouée (HTTP ${r.status}) : ${detail}`;
        setToast({ text, kind: "err" });
        return;
      }
      const out = (await r.json()) as {
        fields_patched: string[];
        model_used: string;
      };
      await onSuccess();
      const n = out.fields_patched?.length || 0;
      setToast({
        text:
          n > 0
            ? `Champs ré-extraits par Claude (${n}). Vérifie les modifications.`
            : "Claude n'a pas trouvé de nouveaux champs à remplir.",
        kind: "ok"
      });
    } catch (e) {
      setToast({
        text: `Ré-extraction Claude échouée : ${(e as Error).message}`,
        kind: "err"
      });
    } finally {
      setBusyKind(null);
    }
  }

  const isBusy = busyKind !== null;

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={() => void runGroq()}
          disabled={!hasSources || isBusy}
          title={
            hasSources
              ? "Relance Groq Llama 3.3 70B sur les sources originales (gratuit)"
              : "Aucune source à ré-extraire — colle une URL/texte ou ajoute un fichier"
          }
          className="inline-flex items-center gap-1 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-1 text-[10px] font-medium text-emerald-300 transition hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busyKind === "groq" ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Sparkles className="h-3 w-3" />
          )}
          Ré-extraire avec Groq
        </button>
        {claudeEnabled === true ? (
          <button
            type="button"
            onClick={() => void runClaude()}
            disabled={!hasSources || isBusy}
            title={
              hasSources
                ? "Relance Claude Sonnet 4.6 (~3¢) — uniquement si tu veux comparer"
                : "Aucune source à ré-extraire"
            }
            className="inline-flex items-center gap-1 rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[10px] font-medium text-amber-300 transition hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busyKind === "claude" ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Sparkles className="h-3 w-3" />
            )}
            Claude (~3¢)
          </button>
        ) : null}
      </div>
      {toast ? (
        <div
          className={`mt-1 flex max-w-[320px] items-start gap-1 rounded-md border px-2 py-1 text-right text-[10px] ${
            toast.kind === "ok"
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
              : "border-rose-500/40 bg-rose-500/10 text-rose-300"
          }`}
        >
          <p className="flex-1 whitespace-pre-line text-left">{toast.text}</p>
          {toast.kind === "err" ? (
            <button
              type="button"
              onClick={() => setToast(null)}
              aria-label="Fermer le message"
              className="ml-1 shrink-0 text-rose-300 hover:text-rose-100"
            >
              ✕
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

