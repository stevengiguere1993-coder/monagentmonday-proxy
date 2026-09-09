"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Building2,
  DoorOpen,
  Loader2,
  Plus,
  Search,
  X
} from "lucide-react";

import { Link, useRouter } from "@/i18n/navigation";
import { authedFetch } from "@/lib/auth";
import { BoutonExport } from "@/components/immobilier/bouton-export";
import { ImmobilierTopbar, useImmobilierLayout } from "../layout";
import { LOUER_INDEFINIMENT_INFO } from "@/components/immobilier/fin-bail";
import {
  fmtPieces,
  LogementFiche,
  type LogementFicheData
} from "@/components/immobilier/logement-fiche";

/**
 * Logements — vue agrégée de TOUS les logements du portefeuille
 * (entreprise active via le contexte du layout). Filtres client-side :
 * recherche texte, immeuble, statut. Clic sur une ligne → PAGE fiche
 * logement (/immobilier/logements/{id}) ; la colonne immeuble reste
 * un lien vers la fiche immeuble.
 */

type ImmeubleLite = {
  id: number;
  name: string;
  address: string;
  city?: string | null;
  gestion_externe?: boolean;
};

type Logement = LogementFicheData;

type Row = Logement & {
  immeuble_name: string;
  immeuble_gestion_externe: boolean;
};

const STATUTS = [
  { value: "all", label: "Tous" },
  { value: "occupe", label: "Occupés" },
  { value: "vacant", label: "Vacants" },
  { value: "reserve", label: "Réservés" },
  { value: "hors_location", label: "Hors loc." }
];

function fmtMoney(n: number | null | undefined): string {
  if (n == null) return "—";
  return new Intl.NumberFormat("fr-CA", {
    style: "currency",
    currency: "CAD",
    maximumFractionDigits: 0
  }).format(n);
}

function fmtJour(iso?: string | null): string {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  // L'année s'affiche dès qu'elle diffère de l'année courante : « libre
  // le 30 juin 2027 » (retour Phil 2026-09-09 sur le 3 Elgin).
  return new Date(y, (m || 1) - 1, d || 1).toLocaleDateString("fr-CA", {
    day: "numeric",
    month: "short",
    ...(y !== new Date().getFullYear() ? { year: "numeric" } : {})
  });
}

function StatutBadge({
  status,
  libreLe
}: {
  status: string;
  /** Départ ACTÉ : le logement se libère à cette date. */
  libreLe?: string | null;
}) {
  const map: Record<string, { cls: string; label: string }> = {
    occupe: { cls: "badge-emerald", label: "Occupé" },
    vacant: { cls: "badge-amber", label: "Vacant" },
    reserve: { cls: "badge-sky", label: "Réservé" },
    hors_location: { cls: "badge-neutral", label: "Hors loc." }
  };
  const t = map[status] || { cls: "badge-neutral", label: status };
  // Un logement occupé dont le départ est acté n'est pas dans le même
  // état qu'un logement occupé tout court : c'est celui-là qu'il faut
  // relouer (retour Phil 2026-08-19).
  if (libreLe && status === "occupe") {
    return (
      <span
        className="badge badge-amber"
        title={`Départ confirmé — le logement se libère le ${libreLe}`}
      >
        Occupé · libre le {fmtJour(libreLe)}
      </span>
    );
  }
  return <span className={`badge ${t.cls}`}>{t.label}</span>;
}

type DoublonGroupe = {
  immeuble_id: number;
  immeuble_name: string;
  numero: string;
  logements: Array<{
    id: number;
    numero: string;
    status: string;
    nb_baux: number;
    nb_paiements_externes: number;
  }>;
};

/** Logements en double dans un même immeuble (retour Phil 2026-09-09 :
 *  « 8906-C » trois fois). Fusion en un clic : tout ce qui est rattaché
 *  aux doublons suit le logement conservé, rien n'est effacé. */
function DoublonsLogementsBanner() {
  const [groupes, setGroupes] = useState<DoublonGroupe[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const charger = useCallback(async () => {
    try {
      const r = await authedFetch("/api/v1/immobilier/logements/doublons");
      if (r.ok) setGroupes((await r.json()) as DoublonGroupe[]);
    } catch {
      /* diagnostic seulement */
    }
  }, []);
  useEffect(() => {
    void charger();
  }, [charger]);
  if (!groupes || groupes.length === 0) return null;

  async function fusionner(g: DoublonGroupe) {
    const ids = g.logements.map((l) => l.id).sort((a, b) => a - b);
    const garder = ids[0];
    if (
      !window.confirm(
        `Fusionner les ${ids.length} logements « ${g.numero} » de ${g.immeuble_name} en un seul (le plus ancien, #${garder}) ? Baux, paiements et documents des doublons seront rattachés au logement conservé.`
      )
    )
      return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await authedFetch("/api/v1/immobilier/logements/fusionner", {
        method: "POST",
        body: JSON.stringify({
          garder_id: garder,
          supprimer_ids: ids.filter((i) => i !== garder)
        })
      });
      if (!r.ok) throw new Error((await r.text()).slice(0, 200));
      setMsg(`« ${g.numero} » fusionné.`);
      await charger();
      window.setTimeout(() => window.location.reload(), 600);
    } catch (e) {
      setMsg(`Fusion impossible : ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-4 rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-xs text-amber-100">
      <p className="font-semibold">
        {groupes.length} numéro{groupes.length > 1 ? "s" : ""} de logement en
        double — la page Paiements les affiche plusieurs fois.
      </p>
      <ul className="mt-2 space-y-1.5">
        {groupes.map((g) => (
          <li
            key={`${g.immeuble_id}-${g.numero}`}
            className="flex flex-wrap items-center gap-2"
          >
            <span>
              {g.immeuble_name} · <strong>{g.numero}</strong> ×
              {g.logements.length}
              <span className="ml-1 text-amber-200/70">
                (
                {g.logements
                  .map(
                    (l) =>
                      `#${l.id} ${l.status}${l.nb_baux ? ` · ${l.nb_baux} bail` : ""}${
                        l.nb_paiements_externes
                          ? ` · ${l.nb_paiements_externes} paiement(s)`
                          : ""
                      }`
                  )
                  .join(" ; ")}
                )
              </span>
            </span>
            <button
              type="button"
              disabled={busy}
              onClick={() => void fusionner(g)}
              className="rounded-md border border-amber-400/60 bg-amber-500/20 px-2 py-0.5 text-[11px] font-semibold text-amber-100 hover:bg-amber-500/30 disabled:opacity-50"
            >
              Fusionner
            </button>
          </li>
        ))}
      </ul>
      {msg ? <p className="mt-2 text-amber-200">{msg}</p> : null}
    </div>
  );
}

export default function LogementsPage() {
  const { currentEntrepriseId } = useImmobilierLayout();
  const router = useRouter();
  const [rows, setRows] = useState<Row[] | null>(null);
  const [immeubles, setImmeubles] = useState<ImmeubleLite[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [immeubleFilter, setImmeubleFilter] = useState<number | "all">("all");
  const [statutFilter, setStatutFilter] = useState<string>("all");

  // « + Ajouter un logement » (même modale que la fiche immeuble) : on
  // choisit d'abord l'immeuble, puis la modale LogementFiche s'ouvre.
  const [chooserOpen, setChooserOpen] = useState(false);
  const [addImmId, setAddImmId] = useState("");
  const [showCreate, setShowCreate] = useState(false);

  // Clic sur une ligne → page fiche logement (vraie page 360).
  function openFiche(row: Row) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    router.push(`/immobilier/logements/${row.id}` as any);
  }

  // Jeton anti-course : seul le chargement le plus récent écrit l'état
  // (changement d'entreprise rapide, rechargement après création).
  const loadToken = useRef(0);
  const load = useCallback(async () => {
    const token = ++loadToken.current;
    setRows(null);
    setError(null);
    try {
      const url =
        currentEntrepriseId != null
          ? `/api/v1/immobilier/immeubles?entreprise_id=${currentEntrepriseId}`
          : "/api/v1/immobilier/immeubles";
      const res = await authedFetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const imms = (await res.json()) as ImmeubleLite[];
      if (token !== loadToken.current) return;
      setImmeubles(imms);

      const lists = await Promise.all(
        imms.map(async (imm) => {
          const r = await authedFetch(
            `/api/v1/immobilier/immeubles/${imm.id}/logements`
          );
          if (!r.ok) return [] as Row[];
          const logs = (await r.json()) as Logement[];
          return logs.map((l) => ({
            ...l,
            immeuble_name: imm.name,
            immeuble_gestion_externe: !!imm.gestion_externe
          }));
        })
      );
      if (token !== loadToken.current) return;
      setRows(lists.flat());
    } catch (err) {
      if (token === loadToken.current)
        setError((err as Error).message);
    }
  }, [currentEntrepriseId]);

  useEffect(() => {
    setImmeubleFilter("all");
    void load();
  }, [load]);

  const filtered = useMemo(() => {
    if (rows === null) return null;
    const q = search.trim().toLowerCase();
    return rows.filter((r) => {
      if (immeubleFilter !== "all" && r.immeuble_id !== immeubleFilter)
        return false;
      if (statutFilter !== "all" && r.status !== statutFilter) return false;
      if (q) {
        const hay = `${r.numero} ${r.immeuble_name} ${r.type}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [rows, search, immeubleFilter, statutFilter]);

  return (
    <>
      <ImmobilierTopbar
        breadcrumbs={[
          { label: "Gestion immobilière", href: "/immobilier" },
          { label: "Logements" }
        ]}
      />

      <div className="p-4 lg:p-6">
        <DoublonsLogementsBanner />
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent-500/15 text-accent-500">
              <DoorOpen className="h-5 w-5" />
            </span>
            <div>
              <h1 className="text-2xl font-bold text-white">Logements</h1>
              <p className="mt-1 max-w-2xl text-sm text-white/60">
                Tous les logements du portefeuille, tous immeubles confondus —
                statut, pièces et loyer en un coup d&apos;œil (loyer du bail
                si occupé, loyer demandé si vacant).
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <BoutonExport
              cibles={[
                {
                  base: "/api/v1/immobilier/exports/logements",
                  sujet: "logements",
                  params: {
                    immeuble_id:
                      immeubleFilter !== "all" ? immeubleFilter : undefined
                  }
                }
              ]}
            />
            <button
              type="button"
              onClick={() => {
                setAddImmId(
                  immeubleFilter !== "all" ? String(immeubleFilter) : ""
                );
                setChooserOpen(true);
              }}
              className="btn-outline-accent btn-sm"
            >
              <Plus className="h-3.5 w-3.5" /> Ajouter un logement
            </button>
          </div>
        </header>

        {/* Filtres */}
        <div className="mt-5 flex flex-wrap items-center gap-2">
          <div className="relative max-w-md flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/40" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Recherche n° de logement / immeuble…"
              className="input w-full pl-9"
            />
          </div>
          <select
            value={immeubleFilter === "all" ? "all" : String(immeubleFilter)}
            onChange={(e) =>
              setImmeubleFilter(
                e.target.value === "all" ? "all" : Number(e.target.value)
              )
            }
            className="input w-auto max-w-[220px] text-sm"
          >
            <option value="all">Tous les immeubles</option>
            {immeubles.map((imm) => (
              <option key={imm.id} value={imm.id}>
                {imm.name}
              </option>
            ))}
          </select>
          {STATUTS.map((s) => (
            <FilterPill
              key={s.value}
              label={s.label}
              active={statutFilter === s.value}
              onClick={() => setStatutFilter(s.value)}
            />
          ))}
          {filtered ? (
            <span className="text-xs text-white/50">
              {filtered.length} / {rows?.length || 0}
            </span>
          ) : null}
        </div>

        {error ? (
          <p className="mt-4 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
            <AlertTriangle className="mr-1.5 inline h-3.5 w-3.5" />
            {error}
          </p>
        ) : null}

        {filtered === null ? (
          <p className="mt-4 text-xs text-white/50">
            <Loader2 className="mr-1 inline h-3 w-3 animate-spin" />{" "}
            Chargement…
          </p>
        ) : filtered.length === 0 ? (
          <p className="mt-4 rounded-lg border border-brand-800 bg-brand-900 px-4 py-3 text-sm text-white/60">
            Aucun logement{" "}
            {rows && rows.length > 0
              ? "correspondant aux filtres"
              : "dans le portefeuille"}
            .
          </p>
        ) : (
          <div className="mt-4 overflow-hidden rounded-2xl border border-brand-800 bg-brand-900">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-left text-sm">
                <thead className="border-b border-brand-800 bg-brand-950 text-[10px] uppercase tracking-wider text-white/50">
                  <tr>
                    <th className="px-4 py-2.5">Logement</th>
                    <th className="px-4 py-2.5">Immeuble</th>
                    <th className="px-4 py-2.5">Type</th>
                    <th className="px-4 py-2.5">Pièces</th>
                    <th className="px-4 py-2.5 text-right">Loyer</th>
                    <th className="px-4 py-2.5 text-right">Statut</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-brand-800">
                  {filtered.map((l) => (
                    <tr
                      key={l.id}
                      onClick={() => openFiche(l)}
                      className="group cursor-pointer hover:bg-brand-950/50"
                    >
                      <td className="px-4 py-3">
                        <span className="flex items-center gap-3">
                          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-500/15 text-accent-500">
                            <DoorOpen className="h-4 w-4" />
                          </span>
                          <span className="font-bold text-white group-hover:text-accent-500">
                            {l.numero}
                          </span>
                        </span>
                      </td>
                      <td className="px-4 py-3 text-xs text-white/70">
                        <Link
                          // eslint-disable-next-line @typescript-eslint/no-explicit-any
                          href={`/immobilier/immeubles/${l.immeuble_id}` as any}
                          onClick={(e) => e.stopPropagation()}
                          className="inline-flex items-center gap-1.5 hover:text-accent-500"
                        >
                          <Building2 className="h-3.5 w-3.5 text-white/40" />
                          {l.immeuble_name}
                        </Link>
                        {l.immeuble_gestion_externe ? (
                          <span className="ml-1.5 badge badge-sky">
                            Gestion externe
                          </span>
                        ) : null}
                      </td>
                      <td className="px-4 py-3 text-xs text-white/60">
                        {l.type}
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-white/70">
                        {l.location_en_chambres ? (
                          <span
                            title={LOUER_INDEFINIMENT_INFO}
                            className="cursor-help border-b border-dotted border-white/25"
                          >
                            Chambre ∞
                          </span>
                        ) : (
                          fmtPieces(l.nb_pieces_decimal)
                        )}
                      </td>
                      <td
                        className="px-4 py-3 text-right font-mono text-xs text-white/80"
                        title={
                          l.immeuble_gestion_externe
                            ? "Loyer saisi sur le logement (gestion externe)"
                            : l.status === "occupe"
                              ? "Loyer du bail actif"
                              : "Loyer demandé (prix de la prochaine location)"
                        }
                      >
                        {/* Hiérarchie du loyer effectif (2026-08-14) :
                            externe → loyer SAISI ; interne occupé →
                            loyer RÉEL du bail ; vacant → demandé. */}
                        {fmtMoney(
                          !l.immeuble_gestion_externe &&
                            l.status === "occupe"
                            ? (l.loyer_actuel ?? l.loyer_demande)
                            : l.loyer_demande
                        )}
                        {!l.immeuble_gestion_externe &&
                        l.status !== "occupe" &&
                        l.loyer_demande != null ? (
                          <span className="ml-1 text-white/40">
                            demandé
                          </span>
                        ) : null}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <StatutBadge status={l.status} libreLe={l.libre_le} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {/* Choix de l'immeuble AVANT la modale de création (la fiche
          logement a besoin de savoir où le créer). */}
      {chooserOpen ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setChooserOpen(false)}
        >
          <div
            className="w-full max-w-sm rounded-2xl border border-brand-800 bg-brand-900 p-5 shadow-card"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-start justify-between gap-3">
              <h3 className="text-base font-bold text-white">
                Ajouter un logement
              </h3>
              <button
                type="button"
                className="rounded-lg p-1.5 text-white/40 hover:text-white"
                onClick={() => setChooserOpen(false)}
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <label className="text-xs font-medium text-white/60">
              Immeuble
            </label>
            <select
              value={addImmId}
              onChange={(e) => setAddImmId(e.target.value)}
              className="input mt-1 w-full"
            >
              <option value="">Choisir…</option>
              {immeubles.map((imm) => (
                <option key={imm.id} value={String(imm.id)}>
                  {imm.name}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={!addImmId}
              onClick={() => {
                setChooserOpen(false);
                setShowCreate(true);
              }}
              className="btn-accent btn-sm mt-4 w-full justify-center disabled:opacity-50"
            >
              <Plus className="h-4 w-4" /> Continuer
            </button>
          </div>
        </div>
      ) : null}

      {showCreate && addImmId ? (
        <LogementFiche
          logement={null}
          immeubleId={Number(addImmId)}
          onClose={() => setShowCreate(false)}
          onSaved={() => {
            setShowCreate(false);
            void load();
          }}
          onDeleted={() => {
            setShowCreate(false);
            void load();
          }}
        />
      ) : null}
    </>
  );
}

function FilterPill({
  label,
  active,
  onClick
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-full px-3 py-1 text-xs font-semibold transition ${
        active
          ? "bg-brand-900 text-white"
          : "border border-white/10 bg-brand-950 text-white/60 hover:text-white"
      }`}
    >
      {label}
    </button>
  );
}
