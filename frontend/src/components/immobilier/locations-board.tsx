"use client";

/**
 * Pipeline « Locations » (relocation / vacances) — KANBAN partagé :
 * - page /immobilier/locations (tous les immeubles, filtre)
 * - onglet « Locations » de la fiche immeuble (prop immeubleId)
 *
 * Location SIMPLIFIÉE (retour Phil 2026-09-09) — quatre colonnes :
 * « À louer » → « Affiché » → « Bail en signature » → « Reloué ».
 * Plus d'annonces, de visites, de candidats ni d'enquêtes ici : ça se
 * passe hors de Kratos. Sur une carte, on LIE un locataire (déjà
 * client, ou créé sur place) : son bail proposé est créé et la carte
 * passe à « Bail en signature ». Le bail signé (PDF du gestionnaire, ou
 * bientôt notre système interne) fait passer la carte à « Reloué ».
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  FileSignature,
  KeyRound,
  Loader2,
  Megaphone,
  Plus,
  ShieldCheck,
  Timer,
  Trash2,
  UserPlus,
  Users,
  X
} from "lucide-react";

import { Link } from "@/i18n/navigation";
import { authedFetch } from "@/lib/auth";
import {
  TalFormDropdown
} from "@/components/immobilier/tal-avis";

type Dossier = {
  id: number;
  logement_id: number;
  logement_numero: string;
  immeuble_id: number;
  immeuble_name: string;
  bail_id: number | null;
  locataire_sortant: string | null;
  statut: string;
  date_depart: string | null;
  loyer_demande: number | null;
  loyer_ancien: number | null;
  reloue_le: string | null;
  notes: string | null;
  depot_sortant: number | null;
  depot_sortant_rendu_le: string | null;
  //: Locataire LIÉ (bail proposé) — null tant qu'on n'a lié personne.
  nouveau_bail_id: number | null;
  nouveau_locataire_id: number | null;
  nouveau_locataire_nom: string | null;
  nouveau_bail_date_debut: string | null;
  nouveau_bail_loyer: number | null;
  //: PDF du bail signé au dossier (null = en attente de signature).
  nouveau_bail_document_id: number | null;
  gestion_externe?: boolean;
};

type Overview = {
  rows: Dossier[];
  nb_actifs: number;
  nb_a_louer: number;
  nb_affiches: number;
  nb_en_signature: number;
  nb_reloues_90j: number;
  jours_vacants_moyens: number | null;
};

const COLUMNS: Array<{
  id: string;
  label: string;
  dot: string;
  hint: string;
}> = [
  {
    id: "avis_recu",
    label: "À louer",
    dot: "bg-amber-400",
    hint: "Départ confirmé ou unité libre — rien n'est encore affiché."
  },
  {
    id: "annonce_publiee",
    label: "Affiché",
    dot: "bg-sky-400",
    hint: "L'annonce est en ligne (Marketplace, Kijiji…) — hors Kratos."
  },
  {
    id: "bail_envoye",
    label: "Bail en signature",
    dot: "bg-fuchsia-400",
    hint: "Un locataire est lié ; son bail proposé attend la signature."
  },
  {
    id: "reloue",
    label: "Reloué",
    dot: "bg-emerald-400",
    hint: "Le bail signé est au dossier."
  }
];

const STATUTS_ACTIFS = ["avis_recu", "annonce_publiee", "bail_envoye"];

const STATUT_LABEL: Record<string, string> = Object.fromEntries(
  COLUMNS.map((c) => [c.id, c.label])
);
STATUT_LABEL.annule = "Annulé";

function money(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString("fr-CA", {
    style: "currency",
    currency: "CAD",
    maximumFractionDigits: 0
  });
}

function fmtDate(d: string | null | undefined): string {
  if (!d) return "—";
  return d.slice(0, 10);
}

//: « 1er juil. », « 15 sept. » — la date courte des cartes compactes.
//: L'année n'apparaît que si ce n'est pas l'année en cours (une carte
//: « Reloué » de l'an passé ne doit pas passer pour un départ proche).
function fmtDateCourte(d: string | null | undefined): string {
  if (!d) return "—";
  const [a, m, j] = d.slice(0, 10).split("-").map(Number);
  if (!a || !m || !j) return d.slice(0, 10);
  const mois = new Date(a, m - 1, j).toLocaleDateString("fr-CA", {
    month: "short"
  });
  const jour = j === 1 ? "1er" : String(j);
  return a === new Date().getFullYear()
    ? `${jour} ${mois}`
    : `${jour} ${mois} ${a}`;
}

function fmtDateTime(d: string | null | undefined): string {
  if (!d) return "À planifier";
  return new Date(d).toLocaleString("fr-CA", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function addMonthsIso(iso: string, months: number): string {
  const d = new Date(`${iso}T00:00:00`);
  d.setMonth(d.getMonth() + months);
  d.setDate(d.getDate() - 1);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

const INPUT_CLS =
  "rounded-md border border-brand-800 bg-brand-950 px-2 py-1.5 text-xs text-white outline-none focus:border-accent-500";

export function LocationsBoard({
  immeubleId,
  entrepriseId
}: {
  immeubleId?: number;
  entrepriseId?: number | null;
}) {
  const [data, setData] = useState<Overview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  //: Dossier dont la fiche doit s'ouvrir DIRECTEMENT sur « Lier un
  //: locataire » — quand on a glissé la carte vers une colonne qui
  //: exige un locataire sans qu'il y en ait encore (le frein FAIT au
  //: lieu de bloquer, retour Phil 2026-08-20).
  const [lierPour, setLierPour] = useState<number | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [importBailFor, setImportBailFor] = useState<Dossier | null>(null);
  const [showHistorique, setShowHistorique] = useState(false);
  const [immeubleFilter, setImmeubleFilter] = useState<number | "all">("all");
  const [dragOverCol, setDragOverCol] = useState<string | null>(null);
  // m3 (audit 2026-08-13) : `?focus={dossier_id}` — venu d'une pastille
  // « Ouvrir dans Locations » (page Baux, fiches) : la carte visée est
  // défilée à l'écran et surlignée quelques secondes.
  const [focusId, setFocusId] = useState<number | null>(null);

  useEffect(() => {
    const raw = new URLSearchParams(window.location.search).get("focus");
    const id = raw ? Number(raw) : NaN;
    if (Number.isFinite(id) && id > 0) setFocusId(id);
  }, []);

  useEffect(() => {
    if (focusId == null || data === null) return;
    const el = document.getElementById(`dossier-card-${focusId}`);
    if (!el) return;
    el.scrollIntoView({
      behavior: "smooth",
      block: "center",
      inline: "center"
    });
    const t = window.setTimeout(() => setFocusId(null), 5000);
    return () => window.clearTimeout(t);
  }, [focusId, data]);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const params = new URLSearchParams();
      if (immeubleId != null) params.set("immeuble_id", String(immeubleId));
      else if (entrepriseId != null)
        params.set("entreprise_id", String(entrepriseId));
      const r = await authedFetch(
        `/api/v1/immobilier/locations/overview?${params.toString()}`
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData((await r.json()) as Overview);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [immeubleId, entrepriseId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function patchDossier(
    id: number,
    body: Record<string, unknown>
  ): Promise<boolean> {
    setErr(null);
    const r = await authedFetch(`/api/v1/immobilier/locations/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body)
    });
    if (!r.ok) {
      const t = await r.text();
      setErr(t.slice(0, 200) || `HTTP ${r.status}`);
      return false;
    }
    await load();
    return true;
  }

  const immeubles = useMemo(() => {
    const m = new Map<number, string>();
    for (const r of data?.rows || []) m.set(r.immeuble_id, r.immeuble_name);
    return [...m.entries()]
      .map(([id, name]) => ({ id, name }))
      .sort((a, b) => a.name.localeCompare(b.name, "fr"));
  }, [data]);

  const rows = useMemo(() => {
    let list = data?.rows || [];
    if (immeubleId == null && immeubleFilter !== "all") {
      list = list.filter((r) => r.immeuble_id === immeubleFilter);
    }
    return list;
  }, [data, immeubleFilter, immeubleId]);

  const byColumn = useMemo(() => {
    const map = Object.fromEntries(
      COLUMNS.map((c) => [c.id, [] as Dossier[]])
    );
    for (const r of rows) {
      if (map[r.statut]) map[r.statut].push(r);
    }
    return map;
  }, [rows]);

  const annules = rows.filter((r) => r.statut === "annule");
  const selected = openId != null ? rows.find((r) => r.id === openId) : null;

  return (
    <div className="space-y-4">
      {/* KPIs — une tuile par colonne + la vacance moyenne */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <KpiTile
          icon={<KeyRound className="h-4 w-4" />}
          label="À louer"
          value={String(data?.nb_a_louer ?? "…")}
          cls="border-amber-500/30 bg-amber-500/5 text-amber-200"
        />
        <KpiTile
          icon={<Megaphone className="h-4 w-4" />}
          label="Affichés"
          value={String(data?.nb_affiches ?? "…")}
          cls="border-sky-500/30 bg-sky-500/5 text-sky-200"
        />
        <KpiTile
          icon={<FileSignature className="h-4 w-4" />}
          label="Bail en signature"
          value={String(data?.nb_en_signature ?? "…")}
          cls="border-fuchsia-500/30 bg-fuchsia-500/5 text-fuchsia-200"
        />
        <KpiTile
          icon={<Timer className="h-4 w-4" />}
          label="Jours vacants (moy.)"
          value={
            data == null
              ? "…"
              : data.jours_vacants_moyens != null
                ? String(data.jours_vacants_moyens)
                : "—"
          }
          cls="border-brand-700 bg-brand-900/40 text-white/80"
        />
        <KpiTile
          icon={<Check className="h-4 w-4" />}
          label="Reloués (90 j)"
          value={String(data?.nb_reloues_90j ?? "…")}
          cls="border-emerald-500/30 bg-emerald-500/5 text-emerald-200"
        />
      </div>

      {/* Barre d'actions */}
      <div className="flex flex-wrap items-center gap-2">
        {immeubleId == null ? (
          <select
            value={immeubleFilter === "all" ? "all" : String(immeubleFilter)}
            onChange={(e) =>
              setImmeubleFilter(
                e.target.value === "all" ? "all" : Number(e.target.value)
              )
            }
            className="input w-auto max-w-[240px] text-sm"
          >
            <option value="all">Tous les immeubles</option>
            {immeubles.map((i) => (
              <option key={i.id} value={i.id}>
                {i.name}
              </option>
            ))}
          </select>
        ) : null}
        <button
          type="button"
          onClick={() => setShowCreate(true)}
          className="btn-outline-accent btn-sm ml-auto"
          title="Ouvre un dossier de relocation sur un logement (départ à venir ou logement déjà vacant). Ne vide PAS le logement : son statut suit ses baux."
        >
          <Plus className="h-3.5 w-3.5" /> Nouvelle relocation
        </button>
      </div>

      {err ? (
        <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {err}
        </p>
      ) : null}

      {/* Kanban */}
      {data === null ? (
        <div className="flex items-center gap-2 py-8 text-xs text-white/50">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Chargement…
        </div>
      ) : (
        <div className="flex gap-3 overflow-x-auto pb-4">
          {COLUMNS.map((col) => {
            const cards = byColumn[col.id] || [];
            return (
              <div
                key={col.id}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragOverCol(col.id);
                }}
                onDragLeave={() =>
                  setDragOverCol((c) => (c === col.id ? null : c))
                }
                onDrop={(e) => {
                  e.preventDefault();
                  setDragOverCol(null);
                  const id = Number(e.dataTransfer.getData("text/plain"));
                  if (!Number.isFinite(id) || id <= 0) return;
                  const d = rows.find((r) => r.id === id);
                  if (!d || d.statut === col.id) return;
                  // Reloué = bail signé importé OBLIGATOIRE.
                  if (col.id === "reloue" && d.nouveau_bail_id) {
                    setImportBailFor(d);
                    return;
                  }
                  // « Bail en signature » / « Reloué » veulent dire
                  // « un locataire est lié » : y arriver sans locataire
                  // donnerait une carte qui ment. Le frein FAIT au lieu
                  // de bloquer (retour Phil 2026-08-20) : on ouvre
                  // « Lier un locataire », la carte avance ensuite
                  // d'elle-même.
                  if (
                    (col.id === "bail_envoye" || col.id === "reloue") &&
                    d.nouveau_bail_id == null
                  ) {
                    setLierPour(d.id);
                    setOpenId(d.id);
                    return;
                  }
                  void patchDossier(id, { statut: col.id });
                }}
                className={`flex w-72 min-w-[270px] flex-shrink-0 flex-col rounded-xl border bg-brand-900/60 transition ${
                  dragOverCol === col.id
                    ? "border-accent-500"
                    : "border-brand-800"
                }`}
              >
                <div className="flex items-center justify-between border-b border-brand-800 px-3 py-2">
                  <div className="flex min-w-0 items-center gap-2">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${col.dot}`} />
                    <h3
                      className="truncate text-sm font-semibold text-white"
                      title={col.hint}
                    >
                      {col.label}
                    </h3>
                  </div>
                  <span className="badge badge-neutral shrink-0">
                    {cards.length}
                  </span>
                </div>
                <div className="flex-1 space-y-1.5 p-2">
                  {cards.length === 0 ? (
                    <p className="py-6 text-center text-xs text-white/40">—</p>
                  ) : (
                    cards.map((d) => (
                      <DossierCard
                        key={d.id}
                        d={d}
                        showImmeuble={immeubleId == null}
                        highlighted={focusId === d.id}
                        onOpen={() => setOpenId(d.id)}
                      />
                    ))
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Annulés (repliés) */}
      {annules.length > 0 ? (
        <div>
          <button
            type="button"
            onClick={() => setShowHistorique((v) => !v)}
            className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-white/40 hover:text-white/70"
          >
            {showHistorique ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
            Annulés ({annules.length})
          </button>
          {showHistorique ? (
            <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              {annules.map((d) => (
                <DossierCard
                  key={d.id}
                  d={d}
                  showImmeuble={immeubleId == null}
                  onOpen={() => setOpenId(d.id)}
                />
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {selected ? (
        <DossierModal
          d={selected}
          lierDemande={lierPour === selected.id}
          onLierConsomme={() => setLierPour(null)}
          onClose={() => {
            setOpenId(null);
            setLierPour(null);
          }}
          onPatch={(body) => patchDossier(selected.id, body)}
          onMutated={() => void load()}
          onError={setErr}
          onDeleted={() => {
            setOpenId(null);
            void load();
          }}
          onImporterBail={(bailId) =>
            setImportBailFor({ ...selected, nouveau_bail_id: bailId })
          }
        />
      ) : null}

      {importBailFor ? (
        <ImportBailSigneModal
          d={importBailFor}
          onClose={() => setImportBailFor(null)}
          onDone={() => {
            setImportBailFor(null);
            void load();
          }}
          onException={async (motif) => {
            const bailId = importBailFor.nouveau_bail_id;
            const dossierId = importBailFor.id;
            const r = await authedFetch(
              `/api/v1/immobilier/baux/${bailId}/exception-document`,
              { method: "POST", body: JSON.stringify({ motif }) }
            );
            if (!r.ok) {
              const t = await r.text();
              throw new Error(t.slice(0, 240) || `HTTP ${r.status}`);
            }
            // L'exception levée, la transition passe le garde-fou.
            await patchDossier(dossierId, { statut: "reloue" });
            setImportBailFor(null);
            void load();
          }}
        />
      ) : null}

      {showCreate ? (
        <CreateDossierModal
          immeubleId={immeubleId}
          onClose={() => setShowCreate(false)}
          onSaved={() => {
            setShowCreate(false);
            void load();
          }}
        />
      ) : null}
    </div>
  );
}

function KpiTile({
  icon,
  label,
  value,
  cls
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  cls: string;
}) {
  return (
    <div className={`rounded-2xl border p-3.5 ${cls}`}>
      <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider opacity-80">
        {icon} {label}
      </div>
      <div className="mt-0.5 text-2xl font-bold">{value}</div>
    </div>
  );
}

// ─── Carte kanban ───────────────────────────────────────────────────────

function DossierCard({
  d,
  showImmeuble,
  onOpen,
  highlighted = false
}: {
  d: Dossier;
  showImmeuble: boolean;
  onOpen: () => void;
  /** Carte ciblée par `?focus={id}` — surlignée après le défilement. */
  highlighted?: boolean;
}) {
  const deltaLoyer =
    d.loyer_demande != null && d.loyer_ancien != null && d.loyer_ancien > 0
      ? ((d.loyer_demande - d.loyer_ancien) / d.loyer_ancien) * 100
      : null;
  const afficherDelta = deltaLoyer != null && Math.abs(deltaLoyer) >= 0.5;

  //: Carte COMPACTE : une seule ligne « Logement 4 · 1 250 $ · 1er juil. »
  //: (tronquée) ; le détail complet — immeuble, locataire sortant, dates
  //: longues, ancien loyer — vit dans le title, la fiche s'ouvre au clic.
  const dateCarte = d.statut === "reloue" ? d.reloue_le : d.date_depart;
  const detail = [
    d.immeuble_name,
    `Logement ${d.logement_numero}`,
    d.statut === "reloue"
      ? `Reloué le ${fmtDate(d.reloue_le)}`
      : `Départ : ${fmtDate(d.date_depart)}`,
    d.locataire_sortant ? `Sortant : ${d.locataire_sortant}` : "",
    d.nouveau_locataire_nom
      ? `Locataire lié : ${d.nouveau_locataire_nom}${
          d.nouveau_bail_document_id ? " (bail signé)" : " (bail à signer)"
        }`
      : "",
    `Loyer demandé : ${money(d.loyer_demande)}${
      d.loyer_ancien != null ? ` (ancien : ${money(d.loyer_ancien)})` : ""
    }`
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <button
      type="button"
      id={`dossier-card-${d.id}`}
      draggable
      onDragStart={(e) =>
        e.dataTransfer.setData("text/plain", String(d.id))
      }
      onClick={onOpen}
      title={detail}
      className={`block w-full cursor-grab rounded-lg border p-2 text-left transition hover:border-accent-500 active:cursor-grabbing ${
        highlighted
          ? "border-accent-500 bg-accent-500/10 ring-2 ring-accent-500/60"
          : "border-brand-800 bg-brand-950"
      }`}
    >
      <p className="truncate text-xs font-semibold text-white">
        Logement {d.logement_numero}
        <span className="font-normal text-white/40"> · </span>
        <span className="font-mono font-normal text-white/80">
          {money(d.loyer_demande)}
        </span>
        {afficherDelta ? (
          <span
            className={`font-normal ${deltaLoyer > 0 ? "text-emerald-300" : "text-rose-300"}`}
          >
            {" "}
            {deltaLoyer > 0 ? "+" : ""}
            {deltaLoyer.toFixed(0)} %
          </span>
        ) : null}
        <span className="font-normal text-white/40"> · </span>
        <span className="font-normal text-white/60">
          {fmtDateCourte(dateCarte)}
        </span>
      </p>
      {showImmeuble ? (
        <p className="truncate text-[11px] text-white/50">{d.immeuble_name}</p>
      ) : null}
      {d.nouveau_locataire_nom ? (
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-white/50">
          <span
            className={`badge max-w-full ${
              d.nouveau_bail_document_id || d.statut === "reloue"
                ? "badge-emerald"
                : "badge-violet"
            }`}
            title={
              d.nouveau_bail_document_id
                ? "Bail signé au dossier"
                : "Bail proposé — en attente de signature"
            }
          >
            <Users className="mr-0.5 inline h-2.5 w-2.5 shrink-0" />
            <span className="min-w-0 truncate">{d.nouveau_locataire_nom}</span>
          </span>
        </div>
      ) : null}
    </button>
  );
}

// ─── Fiche complète (modal) ─────────────────────────────────────────────

function DossierModal({
  d,
  onClose,
  onPatch,
  onMutated,
  onError,
  onDeleted,
  onImporterBail,
  lierDemande,
  onLierConsomme
}: {
  d: Dossier;
  onClose: () => void;
  onPatch: (body: Record<string, unknown>) => Promise<boolean>;
  onMutated: () => void;
  onError: (msg: string) => void;
  onDeleted: () => void;
  /** Ouvre l'import du bail signé (→ « Reloué ») pour ce bail. */
  onImporterBail: (bailId: number) => void;
  /** La carte a été glissée vers une colonne qui exige un locataire :
   *  on ouvre directement « Lier un locataire ». */
  lierDemande?: boolean;
  onLierConsomme?: () => void;
}) {
  const [notesDraft, setNotesDraft] = useState(d.notes || "");
  const [savingNotes, setSavingNotes] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showLier, setShowLier] = useState(false);

  useEffect(() => {
    if (lierDemande) {
      setShowLier(true);
      onLierConsomme?.();
    }
  }, [lierDemande, onLierConsomme]);

  const actif = d.statut !== "reloue" && d.statut !== "annule";

  async function supprimer(): Promise<boolean> {
    setBusy(true);
    try {
      const r = await authedFetch(`/api/v1/immobilier/locations/${d.id}`, {
        method: "DELETE"
      });
      if (!r.ok) {
        const t = await r.text();
        onError(t.slice(0, 200) || `HTTP ${r.status}`);
        return false;
      }
      onMutated();
      return true;
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}
    >
      <div
        className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-brand-800 bg-brand-950 p-6"
        onClick={(e) => e.stopPropagation()}
      >
        {/* En-tête */}
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent-500/15 text-accent-500">
                <KeyRound className="h-4.5 w-4.5" />
              </span>
              <h2 className="text-lg font-bold text-white">
                Logement {d.logement_numero}
              </h2>
              <span
                className={`badge ${
                  d.statut === "reloue"
                    ? "badge-emerald"
                    : d.statut === "annule"
                      ? "badge-neutral"
                      : d.statut === "bail_envoye"
                        ? "badge-violet"
                        : "badge-amber"
                }`}
              >
                {STATUT_LABEL[d.statut] ?? d.statut}
              </span>
            </div>
            <p className="mt-1 text-xs text-white/55">
              {d.immeuble_name}
              {d.locataire_sortant ? ` · ${d.locataire_sortant} quitte` : ""}
              {d.loyer_ancien != null
                ? ` · ancien loyer ${money(d.loyer_ancien)}`
                : ""}
            </p>
          </div>
          <span className="flex items-center gap-2">
            <Link
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              href={`/immobilier/logements/${d.logement_id}` as any}
              className="text-xs text-accent-500 hover:underline"
            >
              Fiche du logement →
            </Link>
            <button
              type="button"
              onClick={onClose}
              className="btn-ghost btn-xs"
            >
              <X className="h-4 w-4" />
            </button>
          </span>
        </div>

        {/* Stepper de statut — cliquer une étape déplace le dossier. */}
        <div className="mt-4 flex items-center gap-0 overflow-x-auto rounded-xl border border-brand-800 bg-brand-900/60 p-1.5">
          {COLUMNS.map((col, i) => {
            const activeIdx = COLUMNS.findIndex((c) => c.id === d.statut);
            const isActive = d.statut === col.id;
            const isPast = activeIdx >= 0 && i < activeIdx;
            return (
              <div key={col.id} className="flex flex-1 items-center">
                {i > 0 ? (
                  <span
                    className={`h-px w-3 flex-shrink-0 sm:w-5 ${
                      isPast || isActive ? "bg-accent-500/60" : "bg-brand-800"
                    }`}
                  />
                ) : null}
                <button
                  type="button"
                  onClick={() => {
                    if (isActive) return;
                    // Même logique que le glisser-déposer : sans
                    // locataire lié, « Bail en signature » et
                    // « Reloué » ouvrent « Lier un locataire » ; avec
                    // un locataire, « Reloué » passe par l'import du
                    // bail signé.
                    if (
                      (col.id === "bail_envoye" || col.id === "reloue") &&
                      d.nouveau_bail_id == null
                    ) {
                      setShowLier(true);
                      return;
                    }
                    if (col.id === "reloue" && d.nouveau_bail_id) {
                      onImporterBail(d.nouveau_bail_id);
                      return;
                    }
                    void onPatch({ statut: col.id });
                  }}
                  title={`${col.hint}\nPasser à « ${col.label} »`}
                  className={`flex min-w-0 flex-1 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg px-2 py-1.5 text-[11px] font-semibold transition ${
                    isActive
                      ? "bg-accent-500/15 text-accent-500 ring-1 ring-inset ring-accent-500/40"
                      : isPast
                        ? "text-white/70 hover:bg-brand-800"
                        : "text-white/35 hover:bg-brand-800 hover:text-white/70"
                  }`}
                >
                  {isPast ? (
                    <Check className="h-3 w-3 text-accent-500/70" />
                  ) : (
                    <span
                      className={`h-1.5 w-1.5 flex-shrink-0 rounded-full ${col.dot} ${
                        isActive ? "" : "opacity-40"
                      }`}
                    />
                  )}
                  {col.label}
                </button>
              </div>
            );
          })}
        </div>

        {/* Champs clés */}
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
          <label className="text-[11px] font-semibold text-white/60">
            Date de départ
            <input
              type="date"
              defaultValue={d.date_depart || ""}
              onBlur={(e) =>
                e.target.value !== (d.date_depart || "")
                  ? void onPatch({ date_depart: e.target.value || null })
                  : undefined
              }
              className={`${INPUT_CLS} mt-0.5 block w-full`}
            />
          </label>
          <label className="text-[11px] font-semibold text-white/60">
            Loyer demandé ($/mois)
            <input
              inputMode="decimal"
              defaultValue={
                d.loyer_demande != null ? String(d.loyer_demande) : ""
              }
              onBlur={(e) => {
                const v = e.target.value.trim();
                const n = v === "" ? null : Number(v);
                if (v !== "" && Number.isNaN(n)) return;
                if (n !== d.loyer_demande) void onPatch({ loyer_demande: n });
              }}
              className={`${INPUT_CLS} mt-0.5 block w-full`}
            />
          </label>
          {d.loyer_ancien != null ? (
            <div className="text-[11px] font-semibold text-white/60">
              Ancien loyer
              <div className="mt-0.5 rounded-md border border-brand-800/60 bg-brand-900/50 px-2 py-1.5 font-mono text-xs text-white/60">
                {money(d.loyer_ancien)}
                {d.loyer_demande != null && d.loyer_ancien > 0 ? (
                  <span
                    className={
                      d.loyer_demande >= d.loyer_ancien
                        ? "ml-1 text-emerald-300"
                        : "ml-1 text-rose-300"
                    }
                  >
                    (
                    {d.loyer_demande >= d.loyer_ancien ? "+" : ""}
                    {(
                      ((d.loyer_demande - d.loyer_ancien) / d.loyer_ancien) *
                      100
                    ).toFixed(0)}
                    &nbsp;%)
                  </span>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>

        {/* Dépôt du locataire sortant (interconnexion Dépôts) */}
        {d.depot_sortant != null ? (
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-violet-500/30 bg-violet-500/10 px-3 py-2 text-xs text-violet-200">
            <ShieldCheck className="h-3.5 w-3.5" />
            Dépôt de garantie du locataire sortant :{" "}
            <strong>{money(d.depot_sortant)}</strong>
            {d.depot_sortant_rendu_le ? (
              <span className="badge badge-emerald">
                Rendu le {fmtDate(d.depot_sortant_rendu_le)}
              </span>
            ) : (
              <span className="badge badge-amber">À rendre au départ</span>
            )}
            <Link
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              href={"/immobilier/depots" as any}
              className="ml-auto text-violet-200 underline-offset-2 hover:underline"
            >
              Gérer dans Dépôts →
            </Link>
          </div>
        ) : null}

        {/* Locataire lié — le cœur de la fiche */}
        {d.nouveau_bail_id == null && actif ? (
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">
            <UserPlus className="h-3.5 w-3.5" />
            Aucun locataire lié pour l&apos;instant.
            <button
              type="button"
              onClick={() => setShowLier(true)}
              className="ml-auto rounded-md border border-emerald-400/40 bg-emerald-500/15 px-2.5 py-1 font-semibold text-emerald-200 hover:bg-emerald-500/25"
            >
              Lier un locataire
            </button>
          </div>
        ) : d.nouveau_bail_id != null && d.statut !== "reloue" ? (
          <div className="mt-3 rounded-lg border border-fuchsia-400/30 bg-fuchsia-500/10 px-3 py-2 text-xs text-fuchsia-100">
            <div className="flex flex-wrap items-center gap-2">
              <FileSignature className="h-3.5 w-3.5" />
              Locataire lié :{" "}
              <strong>{d.nouveau_locataire_nom || "—"}</strong>
              {d.nouveau_bail_date_debut ? (
                <span className="text-fuchsia-200/70">
                  · bail du {fmtDate(d.nouveau_bail_date_debut)}
                </span>
              ) : null}
              {d.nouveau_bail_loyer != null ? (
                <span className="text-fuchsia-200/70">
                  · {money(d.nouveau_bail_loyer)}/mois
                </span>
              ) : null}
              <span className="badge badge-violet ml-auto">
                {d.nouveau_bail_document_id
                  ? "bail signé au dossier"
                  : "en attente du bail signé"}
              </span>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => onImporterBail(d.nouveau_bail_id as number)}
                className="rounded-md border border-emerald-400/40 bg-emerald-500/15 px-2.5 py-1 font-semibold text-emerald-200 hover:bg-emerald-500/25"
              >
                <FileSignature className="mr-1 inline h-3.5 w-3.5" />
                Joindre le bail signé → Reloué
              </button>
              {d.nouveau_locataire_id != null ? (
                <Link
                  // eslint-disable-next-line @typescript-eslint/no-explicit-any
                  href={
                    `/immobilier/locataires/${d.nouveau_locataire_id}` as any
                  }
                  className="rounded-md border border-fuchsia-400/40 bg-fuchsia-500/15 px-2 py-1 text-[11px] font-semibold text-fuchsia-100 hover:bg-fuchsia-500/25"
                >
                  Fiche du locataire
                </Link>
              ) : null}
              <span className="ml-auto">
                <DesistementButton d={d} onDone={onMutated} />
              </span>
            </div>
          </div>
        ) : d.statut === "reloue" && d.nouveau_bail_id ? (
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">
            <Check className="h-3.5 w-3.5" /> Reloué
            {d.nouveau_locataire_nom ? (
              <>
                {" "}
                à <strong>{d.nouveau_locataire_nom}</strong>
              </>
            ) : null}{" "}
            — bail au dossier.
            <TalFormDropdown bailId={d.nouveau_bail_id} />
            {d.nouveau_locataire_id != null ? (
              <Link
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                href={
                  `/immobilier/locataires/${d.nouveau_locataire_id}` as any
                }
                className="rounded-md border border-emerald-400/40 bg-emerald-500/15 px-2 py-1 text-[11px] font-semibold text-emerald-200 hover:bg-emerald-500/25"
              >
                Fiche du locataire
              </Link>
            ) : null}
            <Link
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              href={
                `/immobilier/immeubles/${d.immeuble_id}?tab=baux&bail=${d.nouveau_bail_id}` as any
              }
              className="ml-auto underline-offset-2 hover:underline"
            >
              Voir le bail →
            </Link>
          </div>
        ) : null}

        {actif ? (
          <p className="mt-3 rounded-lg border border-brand-800 bg-brand-950/60 px-3 py-2 text-[11px] text-white/50">
            📄 Le bail signé (PDF du gestionnaire — ou bientôt notre
            système de signature) se joint depuis cette fiche ou en
            glissant la carte à{" "}
            <b className="text-white/75">« Reloué »</b>. C&apos;est ce qui
            garantit qu&apos;aucun locataire n&apos;entre sans bail au
            dossier.
          </p>
        ) : null}

        {/* Notes de suivi — EN BAS (retour Phil 2026-08-19) : ce sont
            des remarques d'appoint. */}
        <div className="mt-4 rounded-xl border border-brand-800 bg-brand-900 p-3.5">
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-accent-500">
            Notes de suivi
          </h4>
          <textarea
            rows={4}
            value={notesDraft}
            onChange={(e) => setNotesDraft(e.target.value)}
            placeholder="État du logement, peinture à faire, où l'annonce est affichée…"
            className="block w-full rounded-md border border-brand-800 bg-brand-950 px-3 py-2 text-xs text-white outline-none focus:border-accent-500"
          />
          <button
            type="button"
            disabled={savingNotes || notesDraft === (d.notes || "")}
            onClick={async () => {
              setSavingNotes(true);
              await onPatch({
                notes: notesDraft.trim() ? notesDraft : null
              });
              setSavingNotes(false);
            }}
            className="btn-secondary btn-sm mt-1.5 disabled:opacity-40"
          >
            {savingNotes ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Check className="h-3.5 w-3.5" />
            )}
            Enregistrer
          </button>
        </div>

        {/* Pied de fiche : actions destructives discrètes */}
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-brand-800 pt-3">
          <button
            type="button"
            disabled={busy}
            title="Efface DÉFINITIVEMENT le dossier — pour un dossier créé par erreur"
            onClick={async () => {
              if (
                window.confirm(
                  d.nouveau_bail_id != null && d.statut !== "reloue"
                    ? "⚠️ Supprimer DÉFINITIVEMENT ce dossier ?\n\nLe bail proposé du locataire lié sera SUPPRIMÉ lui aussi (et sa fiche, si elle a été créée ici)."
                    : "Supprimer DÉFINITIVEMENT ce dossier de relocation ? Pour garder une trace, utilise plutôt « Annuler »."
                )
              ) {
                if (await supprimer()) onDeleted();
              }
            }}
            className="inline-flex items-center gap-1 text-xs text-rose-300/60 hover:text-rose-300 disabled:opacity-50"
          >
            <Trash2 className="h-3 w-3" /> Supprimer définitivement
          </button>
          {actif ? (
            <button
              type="button"
              onClick={() => {
                if (
                  window.confirm(
                    "Annuler ce dossier de relocation ? La carte passe dans « Annulés » (trace conservée)."
                  )
                )
                  void onPatch({ statut: "annule" });
              }}
              className="text-xs text-white/40 hover:text-white/70"
            >
              Annuler le dossier
            </button>
          ) : null}
          <button
            type="button"
            onClick={onClose}
            className="btn-secondary btn-sm ml-auto"
          >
            Fermer
          </button>
        </div>
      </div>

      {showLier ? (
        <LierLocataireModal
          d={d}
          onClose={() => setShowLier(false)}
          onDone={() => {
            setShowLier(false);
            onMutated();
          }}
          onImporterBail={(bailId) => {
            setShowLier(false);
            onMutated();
            onImporterBail(bailId);
          }}
        />
      ) : null}
    </div>
  );
}

// ─── « Lier un locataire » (existant ou créé sur place) ─────────────────

function LierLocataireModal({
  d,
  onClose,
  onDone,
  onImporterBail
}: {
  d: Dossier;
  onClose: () => void;
  onDone: () => void;
  /** Après la liaison : joindre tout de suite le bail signé (PDF). */
  onImporterBail: (bailId: number) => void;
}) {
  const defaultDebut = (() => {
    if (d.date_depart) {
      const dt = new Date(`${d.date_depart}T00:00:00`);
      dt.setDate(dt.getDate() + 1);
      const pad = (n: number) => String(n).padStart(2, "0");
      return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`;
    }
    return new Date().toISOString().slice(0, 10);
  })();

  const [nom, setNom] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [dateNaissance, setDateNaissance] = useState("");
  const [nas, setNas] = useState("");
  const [ancienneAdresse, setAncienneAdresse] = useState("");
  //: Dépôt de l'ANCIEN locataire encore détenu sur ce logement. Phil a
  //: refusé un verrou ici (« ça peut être un petit peu gossant ») : on
  //: le RAPPELLE au bon moment, sans jamais empêcher la relocation.
  const [depotADue, setDepotADue] = useState<{
    montant: number;
    locataire: string | null;
  } | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const r = await authedFetch(
          `/api/v1/immobilier/depots/overview?immeuble_id=${d.immeuble_id}`
        );
        if (!r.ok) return;
        const data = (await r.json()) as {
          rows: {
            logement_id: number | null;
            statut: string;
            montant: number;
            locataire_name: string | null;
          }[];
        };
        const ligne = (data.rows || []).find(
          (x) => x.statut === "a_rendre" && x.logement_id === d.logement_id
        );
        if (ligne) {
          setDepotADue({
            montant: ligne.montant,
            locataire: ligne.locataire_name
          });
        }
      } catch {
        /* le rappel est un plus : son absence ne bloque rien */
      }
    })();
  }, [d.immeuble_id, d.logement_id]);
  // Locataire EXISTANT (déjà client) par défaut : le bail s'attache à
  // sa fiche — zéro doublon. « Nouveau » crée la fiche sur place.
  const [modeExistant, setModeExistant] = useState(true);
  const [locatairesDispo, setLocatairesDispo] = useState<
    { id: number; full_name: string }[] | null
  >(null);
  const [locExistantId, setLocExistantId] = useState<number | null>(null);
  const [locSearch, setLocSearch] = useState("");
  const [employeur, setEmployeur] = useState("");
  const [revenu, setRevenu] = useState("");
  const [debut, setDebut] = useState(defaultDebut);
  const [fin, setFin] = useState(addMonthsIso(defaultDebut, 12));
  const [loyer, setLoyer] = useState(
    d.loyer_demande != null ? String(d.loyer_demande) : ""
  );
  const [depot, setDepot] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<{
    locataire_id: number;
    bail_id: number;
    immeuble_id: number;
  } | null>(null);

  useEffect(() => {
    if (!modeExistant || locatairesDispo !== null) return;
    void (async () => {
      try {
        const r = await authedFetch("/api/v1/immobilier/locataires");
        if (r.ok)
          setLocatairesDispo(
            (await r.json()) as { id: number; full_name: string }[]
          );
        else setLocatairesDispo([]);
      } catch {
        setLocatairesDispo([]);
      }
    })();
  }, [modeExistant, locatairesDispo]);

  async function submit() {
    setSaving(true);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/locations/${d.id}/lier-locataire`,
        {
          method: "POST",
          body: JSON.stringify({
            locataire_id: modeExistant ? locExistantId : null,
            locataire_nom: nom.trim(),
            locataire_email: email.trim() || null,
            locataire_phone: phone.trim() || null,
            date_naissance: dateNaissance || null,
            nas_last4: nas.trim() || null,
            ancienne_adresse: ancienneAdresse.trim() || null,
            employeur: employeur.trim() || null,
            revenu_annuel: revenu.trim() ? Number(revenu) : null,
            date_debut: debut,
            date_fin: fin,
            loyer_mensuel: Number(loyer),
            depot_garantie: depot.trim() ? Number(depot) : null
          })
        }
      );
      if (!r.ok) {
        const t = await r.text();
        throw new Error(t.slice(0, 240) || `HTTP ${r.status}`);
      }
      setDone(
        (await r.json()) as {
          locataire_id: number;
          bail_id: number;
          immeuble_id: number;
        }
      );
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="my-8 w-full max-w-md rounded-2xl border border-brand-800 bg-brand-950 shadow-2xl">
        <div className="flex items-center justify-between border-b border-brand-800 px-5 py-3">
          <h2 className="text-sm font-bold uppercase tracking-wider text-emerald-300">
            Lier un locataire
          </h2>
          <button type="button" onClick={onClose} className="btn-ghost btn-xs">
            <X className="h-4 w-4" />
          </button>
        </div>
        {done ? (
          <div className="space-y-3 p-5 text-sm text-white/80">
            <p className="flex items-center gap-2 font-semibold text-emerald-300">
              <Check className="h-4 w-4" /> Locataire lié — bail proposé
              créé.
            </p>
            <p className="rounded-lg border border-fuchsia-400/30 bg-fuchsia-500/10 px-3 py-2 text-xs text-fuchsia-200">
              La carte passe à « Bail en signature ». Dès que le bail
              signé est en main (PDF du gestionnaire, ou bientôt notre
              système de signature), joins-le : c&apos;est ce qui fait
              passer la carte à « Reloué ».
            </p>
            <div className="flex flex-col gap-1.5 text-xs">
              <Link
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                href={`/immobilier/locataires/${done.locataire_id}` as any}
                className="inline-flex w-fit items-center rounded-md border border-emerald-400/40 bg-emerald-500/15 px-2.5 py-1 font-semibold text-emerald-200 hover:bg-emerald-500/25"
              >
                Fiche du locataire
              </Link>
            </div>
            <div className="flex flex-wrap justify-end gap-2 border-t border-brand-800 pt-3">
              <button
                type="button"
                onClick={onDone}
                className="btn-secondary btn-sm"
              >
                Plus tard
              </button>
              <button
                type="button"
                onClick={() => onImporterBail(done.bail_id)}
                className="btn-accent btn-sm"
              >
                <FileSignature className="h-4 w-4" /> Joindre le bail
                signé maintenant
              </button>
            </div>
          </div>
        ) : (
          <div className="grid gap-3 p-5">
            <p className="rounded-lg border border-sky-400/30 bg-sky-500/10 px-3 py-2 text-xs text-sky-200">
              Choisis un locataire déjà client ou crée sa fiche sur
              place. Son bail est créé en « proposé » — rien n&apos;est
              envoyé au locataire, rien n&apos;est créé sans ton accord.
            </p>
            <div className="flex gap-1.5">
              <button
                type="button"
                onClick={() => setModeExistant(true)}
                className={`rounded-md border px-2.5 py-1 text-xs font-semibold ${
                  modeExistant
                    ? "border-emerald-400/40 bg-emerald-500/15 text-emerald-200"
                    : "border-brand-700 text-white/50 hover:bg-brand-900"
                }`}
              >
                Locataire existant (déjà client)
              </button>
              <button
                type="button"
                onClick={() => setModeExistant(false)}
                className={`rounded-md border px-2.5 py-1 text-xs font-semibold ${
                  !modeExistant
                    ? "border-emerald-400/40 bg-emerald-500/15 text-emerald-200"
                    : "border-brand-700 text-white/50 hover:bg-brand-900"
                }`}
              >
                Nouveau locataire
              </button>
            </div>
            {modeExistant ? (
              <div className="grid gap-2">
                <label className="text-[11px] font-semibold text-white/60">
                  Rechercher le locataire
                  <input
                    value={locSearch}
                    onChange={(e) => setLocSearch(e.target.value)}
                    placeholder="Nom du locataire déjà client…"
                    className={`${INPUT_CLS} mt-0.5 block w-full`}
                  />
                </label>
                <div className="max-h-40 overflow-y-auto rounded-lg border border-brand-800">
                  {(locatairesDispo || [])
                    .filter((l) =>
                      l.full_name
                        .toLowerCase()
                        .includes(locSearch.toLowerCase())
                    )
                    .slice(0, 30)
                    .map((l) => (
                      <button
                        key={l.id}
                        type="button"
                        onClick={() => setLocExistantId(l.id)}
                        className={`block w-full px-3 py-1.5 text-left text-xs ${
                          locExistantId === l.id
                            ? "bg-emerald-500/20 font-semibold text-emerald-200"
                            : "text-white/75 hover:bg-brand-900"
                        }`}
                      >
                        {l.full_name}
                      </button>
                    ))}
                  {locatairesDispo === null ? (
                    <p className="px-3 py-2 text-xs text-white/40">
                      Chargement…
                    </p>
                  ) : null}
                </div>
                <p className="text-[10px] text-white/40">
                  Le bail s&apos;attachera à sa fiche existante —
                  historique conservé, aucun doublon.
                </p>
              </div>
            ) : (
              <>
            <label className="text-[11px] font-semibold text-white/60">
              Nom complet du locataire
              <input
                value={nom}
                onChange={(e) => setNom(e.target.value)}
                className={`${INPUT_CLS} mt-0.5 block w-full`}
              />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="text-[11px] font-semibold text-white/60">
                Courriel *
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
              <label className="text-[11px] font-semibold text-white/60">
                Téléphone *
                <input
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
            </div>
            {depotADue ? (
              /* Rappel, pas verrou : le dépôt de l'ancien locataire est
                 dû dès son départ, et c'est l'oubli le plus courant
                 (« il oublie tout le temps de venir l'enlever à la
                 fin »). On le dit ici, au moment où on pense au
                 logement — la relocation n'est jamais bloquée. */
              <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-100/85">
                💰 Le dépôt de{" "}
                <b>{depotADue.locataire || "l'ancien locataire"}</b> (
                {new Intl.NumberFormat("fr-CA", {
                  style: "currency",
                  currency: "CAD",
                  maximumFractionDigits: 0
                }).format(depotADue.montant)}
                ) n&apos;a pas encore été remboursé pour ce logement. Ça
                n&apos;empêche pas la relocation — mais il lui est dû.
              </p>
            ) : null}
            <div className="grid grid-cols-2 gap-3">
              <label className="text-[11px] font-semibold text-white/60">
                Date de naissance *
                <input
                  type="date"
                  value={dateNaissance}
                  onChange={(e) => setDateNaissance(e.target.value)}
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
              <label className="text-[11px] font-semibold text-white/60">
                NAS (4 derniers chiffres)
                <input
                  inputMode="numeric"
                  maxLength={4}
                  value={nas}
                  onChange={(e) =>
                    setNas(e.target.value.replace(/[^0-9]/g, "").slice(0, 4))
                  }
                  placeholder="Optionnel"
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
            </div>
            <label className="text-[11px] font-semibold text-white/60">
              Ancienne adresse
              <input
                value={ancienneAdresse}
                onChange={(e) => setAncienneAdresse(e.target.value)}
                placeholder="Adresse actuelle du locataire (avant le déménagement)"
                className={`${INPUT_CLS} mt-0.5 block w-full`}
              />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="text-[11px] font-semibold text-white/60">
                Employeur
                <input
                  value={employeur}
                  onChange={(e) => setEmployeur(e.target.value)}
                  placeholder="Optionnel"
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
              <label className="text-[11px] font-semibold text-white/60">
                Revenu annuel ($)
                <input
                  inputMode="decimal"
                  value={revenu}
                  onChange={(e) => setRevenu(e.target.value)}
                  placeholder="Optionnel"
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
            </div>
              </>
            )}
            <div className="grid grid-cols-2 gap-3">
              <label className="text-[11px] font-semibold text-white/60">
                Début du bail
                <input
                  type="date"
                  value={debut}
                  onChange={(e) => {
                    setDebut(e.target.value);
                    if (e.target.value)
                      setFin(addMonthsIso(e.target.value, 12));
                  }}
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
              <label className="text-[11px] font-semibold text-white/60">
                Fin du bail
                <input
                  type="date"
                  value={fin}
                  onChange={(e) => setFin(e.target.value)}
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <label className="text-[11px] font-semibold text-white/60">
                Loyer mensuel ($)
                <input
                  inputMode="decimal"
                  value={loyer}
                  onChange={(e) => setLoyer(e.target.value)}
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
              <label className="text-[11px] font-semibold text-white/60">
                Dépôt de garantie ($)
                <input
                  inputMode="decimal"
                  value={depot}
                  onChange={(e) => setDepot(e.target.value)}
                  placeholder="Optionnel"
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
            </div>
            {err ? (
              <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                {err}
              </p>
            ) : null}
            <div className="flex justify-end gap-2 border-t border-brand-800 pt-3">
              <button
                type="button"
                onClick={onClose}
                className="btn-secondary btn-sm"
              >
                Annuler
              </button>
              <button
                type="button"
                disabled={
                  saving ||
                  (modeExistant
                    ? locExistantId == null
                    : !nom.trim() ||
                      !email.trim() ||
                      !phone.trim() ||
                      !dateNaissance) ||
                  !debut ||
                  !fin ||
                  loyer.trim() === "" ||
                  Number.isNaN(Number(loyer))
                }
                onClick={() => void submit()}
                className="btn-accent btn-sm disabled:opacity-60"
              >
                {saving ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <FileSignature className="h-4 w-4" />
                )}
                Lier et créer le bail
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Bail : import du bail signé, retrait du locataire ──────────────────

function ImportBailSigneModal({
  d,
  onClose,
  onDone,
  onException
}: {
  d: Dossier;
  onClose: () => void;
  onDone: () => void;
  //: Déclare l'exception puis fait avancer le dossier — l'import, lui,
  //: passe à « Reloué » tout seul côté serveur.
  onException: (motif: string) => Promise<void>;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [dateEntree, setDateEntree] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  //: Volet EXCEPTION, replié par défaut : le bail reste la règle, on
  //: ne met pas les deux chemins sur le même pied.
  const [exception, setException] = useState(false);
  const [motif, setMotif] = useState("");

  async function declarer() {
    const m = motif.trim();
    if (m.length < 3) return;
    setBusy(true);
    setErr(null);
    try {
      await onException(m);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function importer() {
    if (!file || !d.nouveau_bail_id) return;
    setBusy(true);
    setErr(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      if (dateEntree) fd.append("date_entree", dateEntree);
      const r = await authedFetch(
        `/api/v1/immobilier/baux/${d.nouveau_bail_id}/document`,
        { method: "POST", body: fd }
      );
      if (!r.ok) {
        const t = await r.text();
        throw new Error(t.slice(0, 240) || `HTTP ${r.status}`);
      }
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="my-8 w-full max-w-md rounded-2xl border border-brand-800 bg-brand-950 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-brand-800 px-5 py-3">
          <h2 className="text-sm font-bold uppercase tracking-wider text-emerald-300">
            Importer le bail signé
          </h2>
          <button type="button" onClick={onClose} className="btn-ghost btn-xs">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="grid gap-3 p-5">
          <p className="rounded-lg border border-sky-400/30 bg-sky-500/10 px-3 py-2 text-xs text-sky-200">
            Le bail signé (PDF) est obligatoire pour passer à
            « Reloué » : il devient le bail au dossier — visible dans
            les Documents et les boutons Bail — et l&apos;appartement
            est considéré loué par ce locataire.
          </p>
          <label className="text-[11px] font-semibold text-white/60">
            Bail signé (PDF)
            <input
              type="file"
              accept="application/pdf"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="mt-0.5 block w-full text-xs text-white/70 file:mr-2 file:rounded-md file:border-0 file:bg-brand-800 file:px-2.5 file:py-1.5 file:text-xs file:font-semibold file:text-white"
            />
          </label>
          <label className="text-[11px] font-semibold text-white/60">
            Date d&apos;entrée en vigueur (optionnel)
            <input
              type="date"
              value={dateEntree}
              onChange={(e) => setDateEntree(e.target.value)}
              className={`${INPUT_CLS} mt-0.5 block w-full`}
            />
          </label>
          {err ? (
            <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
              {err}
            </p>
          ) : null}
          <div className="flex justify-end gap-2 border-t border-brand-800 pt-3">
            <button
              type="button"
              onClick={onClose}
              className="btn-secondary btn-sm"
            >
              Annuler
            </button>
            <button
              type="button"
              disabled={busy || !file}
              onClick={() => void importer()}
              className="btn-accent btn-sm disabled:opacity-60"
            >
              {busy ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <FileSignature className="h-4 w-4" />
              )}
              Importer — passe à « Reloué »
            </button>
          </div>

          {/* Échappatoire ASSUMÉE : il existe de vrais cas sans bail à
              joindre, et bloquer sec ferait perdre plus que ça protège.
              Elle est repliée, coûte un motif écrit, et reste signée —
              un oubli ne s'y glisse pas sans qu'on l'ait voulu. */}
          {exception ? (
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">
              <p className="mb-2 text-xs font-semibold text-amber-200">
                ⚠️ Exception — aucun bail à joindre
              </p>
              <p className="mb-2 text-[11px] text-amber-100/70">
                Le dossier passera à « Reloué » sans bail. Sans lui, tu
                n&apos;as aucune preuve du loyer ni des conditions
                convenues. Le motif ci-dessous reste au dossier, avec
                ton nom et la date.
              </p>
              <input
                type="text"
                value={motif}
                onChange={(e) => setMotif(e.target.value)}
                maxLength={255}
                placeholder="Pourquoi n'y a-t-il pas de bail ? (obligatoire)"
                className={`${INPUT_CLS} w-full`}
              />
              <div className="mt-2 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setException(false)}
                  className="btn-ghost btn-xs"
                >
                  Revenir à l&apos;import
                </button>
                <button
                  type="button"
                  disabled={busy || motif.trim().length < 3}
                  onClick={() => void declarer()}
                  className="btn-secondary btn-sm disabled:opacity-50"
                >
                  {busy ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : null}
                  Déclarer l&apos;exception et passer à « Reloué »
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setException(true)}
              className="text-left text-[11px] text-white/40 underline decoration-dotted underline-offset-2 hover:text-white/70"
            >
              Il n&apos;y a vraiment aucun bail à joindre — déclarer une
              exception
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function DesistementButton({
  d,
  onDone
}: {
  d: Dossier;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function desister() {
    if (
      !window.confirm(
        "⚠️ Retirer le locataire lié ?\n\nSon bail proposé est SUPPRIMÉ (et sa fiche aussi, si elle a été créée ici et n'a aucun autre bail). Le dossier revient à « À louer » et le logement reprend son vrai statut."
      )
    )
      return;
    setBusy(true);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/locations/${d.id}/desistement`,
        { method: "POST" }
      );
      if (!r.ok) {
        const t = await r.text();
        throw new Error(t.slice(0, 240) || `HTTP ${r.status}`);
      }
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        disabled={busy}
        onClick={() => void desister()}
        title="Le locataire se désiste ou tu t'es trompé de fiche : supprime le bail proposé (et la fiche créée ici), retour à « À louer »"
        className="rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-[11px] font-semibold text-rose-300 transition hover:bg-rose-500/20 disabled:opacity-50"
      >
        {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
        Retirer le locataire
      </button>
      {err ? (
        <span className="text-[10px] text-rose-300" title={err}>
          {err.slice(0, 80)}
        </span>
      ) : null}
    </>
  );
}

// ─── Création d'un dossier ──────────────────────────────────────────────

type ImmeubleLite = { id: number; name: string };
type LogementLite = {
  id: number;
  numero: string;
  loyer_demande: number | null;
};

function CreateDossierModal({
  immeubleId,
  onClose,
  onSaved
}: {
  immeubleId?: number;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [immeubles, setImmeubles] = useState<ImmeubleLite[]>([]);
  const [immId, setImmId] = useState<number | null>(immeubleId ?? null);
  const [logements, setLogements] = useState<LogementLite[]>([]);
  const [logementId, setLogementId] = useState<number | null>(null);
  const [dateDepart, setDateDepart] = useState("");
  const [loyer, setLoyer] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (immeubleId != null) return;
    void (async () => {
      const r = await authedFetch("/api/v1/immobilier/immeubles");
      if (r.ok) setImmeubles((await r.json()) as ImmeubleLite[]);
    })();
  }, [immeubleId]);

  useEffect(() => {
    setLogementId(null);
    setLogements([]);
    if (immId == null) return;
    void (async () => {
      const r = await authedFetch(
        `/api/v1/immobilier/immeubles/${immId}/logements`
      );
      if (r.ok) setLogements((await r.json()) as LogementLite[]);
    })();
  }, [immId]);

  async function submit() {
    if (logementId == null) {
      setErr("Choisis le logement à relouer.");
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      const r = await authedFetch("/api/v1/immobilier/locations", {
        method: "POST",
        body: JSON.stringify({
          logement_id: logementId,
          date_depart: dateDepart || null,
          loyer_demande: loyer.trim() ? Number(loyer) : null,
          notes: notes.trim() || null
        })
      });
      if (!r.ok) {
        const t = await r.text();
        throw new Error(t.slice(0, 200) || `HTTP ${r.status}`);
      }
      onSaved();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm">
      <div className="my-8 w-full max-w-md rounded-2xl border border-brand-800 bg-brand-950 shadow-2xl">
        <div className="flex items-center justify-between border-b border-brand-800 px-5 py-3">
          <h2 className="text-sm font-bold uppercase tracking-wider text-accent-500">
            Nouvelle relocation
          </h2>
          <button type="button" onClick={onClose} className="btn-ghost btn-xs">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="grid gap-3 p-5">
          {immeubleId == null ? (
            <label className="text-[11px] font-semibold text-white/60">
              Immeuble
              <select
                value={immId == null ? "" : String(immId)}
                onChange={(e) =>
                  setImmId(e.target.value ? Number(e.target.value) : null)
                }
                className={`${INPUT_CLS} mt-0.5 block w-full`}
              >
                <option value="">Choisir…</option>
                {immeubles.map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <label className="text-[11px] font-semibold text-white/60">
            Logement
            <select
              value={logementId == null ? "" : String(logementId)}
              onChange={(e) => {
                const id = e.target.value ? Number(e.target.value) : null;
                setLogementId(id);
                const lg = logements.find((l) => l.id === id);
                if (lg?.loyer_demande != null && !loyer.trim())
                  setLoyer(String(lg.loyer_demande));
              }}
              disabled={immId == null}
              className={`${INPUT_CLS} mt-0.5 block w-full disabled:opacity-50`}
            >
              <option value="">
                {immId == null ? "Choisis d'abord l'immeuble" : "Choisir…"}
              </option>
              {logements.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.numero}
                </option>
              ))}
            </select>
          </label>
          <label className="text-[11px] font-semibold text-white/60">
            Date de départ prévue
            <input
              type="date"
              value={dateDepart}
              onChange={(e) => setDateDepart(e.target.value)}
              className={`${INPUT_CLS} mt-0.5 block w-full`}
            />
          </label>
          <label className="text-[11px] font-semibold text-white/60">
            Loyer demandé ($/mois)
            <input
              inputMode="decimal"
              value={loyer}
              onChange={(e) => setLoyer(e.target.value)}
              className={`${INPUT_CLS} mt-0.5 block w-full`}
            />
          </label>
          <label className="text-[11px] font-semibold text-white/60">
            Notes
            <textarea
              rows={2}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className={`${INPUT_CLS} mt-0.5 block w-full`}
            />
          </label>
          {err ? (
            <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
              {err}
            </p>
          ) : null}
          <div className="flex justify-end gap-2 border-t border-brand-800 pt-3">
            <button
              type="button"
              onClick={onClose}
              className="btn-secondary btn-sm"
            >
              Annuler
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => void submit()}
              className="btn-accent btn-sm disabled:opacity-60"
            >
              {saving ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Plus className="h-4 w-4" />
              )}
              Créer le dossier
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
