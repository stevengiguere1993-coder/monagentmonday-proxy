"use client";

/**
 * Page « Baux » (/immobilier/baux) — split de l'ancienne
 * « Baux & paiements » (v15/v16). La collecte des loyers, elle, vit sur
 * la page « Paiements » (/immobilier/paiements).
 *
 * Une ligne par LOGEMENT, façon Suivis annuels :
 *   - ROUGE : entente de résiliation envoyée, signature attendue ;
 *   - VERTE : bail actif au dossier (PDF importé) ;
 *   - ambre : bail actif mais document à importer ;
 *   - grise : aucun bail — « Créer un nouveau bail » ou importer.
 * Interconnectée au kanban Locations : le statut de relocation s'AFFICHE
 * ici (pastille lecture seule) mais se MODIFIE à la source — la page
 * Locations (retour Phil 2026-08-13).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ClipboardList,
  FileDown,
  Loader2,
  Plus,
  Search,
  Trash2
} from "lucide-react";

import { useSearchParams } from "next/navigation";

import { Link } from "@/i18n/navigation";
import { authedFetch } from "@/lib/auth";
import { ImmobilierTopbar } from "../layout";
import { BandeauAvisRenouvellement } from "@/components/immobilier/bandeau-avis";
import { BandeauBailManquant } from "@/components/immobilier/bandeau-bail-manquant";
import { BandeauDepotARembourser } from "@/components/immobilier/bandeau-depot";
import { BailDocActions } from "@/components/immobilier/tal-avis";
import { BoutonExport } from "@/components/immobilier/bouton-export";
import {
  CreerBailModal,
  AnnulerDepartModal,
  FinBailModal,
  ResiliationSuivi,
  JourEcheanceInline,
  RelocationStatutPastille,
  type SuiviBailRow
} from "@/components/immobilier/fin-bail";
import { RENOUVELLEMENT_BADGES } from "@/components/immobilier/paiements-actions";
import { TableauSuiviBaux } from "@/components/immobilier/tableau-suivi-baux";

type Row = SuiviBailRow;

function money(n: number | null | undefined): string {
  if (n == null) return "—";
  return `${Math.round(n).toLocaleString("fr-CA")} $`;
}

export default function BauxPage() {
  // La page sert aussi de sous-page « Baux & locataires » de la fiche
  // immeuble (?immeuble_id=X) : le bandeau d'avis se limite alors aux
  // alertes de CET immeuble ; sans paramètre → toutes les alertes.
  const searchParams = useSearchParams();
  const immeubleIdParam = searchParams.get("immeuble_id");
  const immeubleId =
    immeubleIdParam != null && /^\d+$/.test(immeubleIdParam)
      ? Number(immeubleIdParam)
      : null;
  const [rows, setRows] = useState<Row[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [fImmeuble, setFImmeuble] = useState("");
  const [search, setSearch] = useState("");

  const load = useCallback(async () => {
    setErr(null);
    try {
      const r = await authedFetch("/api/v1/immobilier/suivi-baux");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setRows((await r.json()) as Row[]);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);


  // Ouvre un document conservé (l'avis courant) dans un nouvel onglet.

  const immeubles = useMemo(() => {
    const m = new Map<number, string>();
    for (const r of rows || []) m.set(r.immeuble_id, r.immeuble_name);
    return [...m.entries()]
      .map(([id, name]) => ({ id, name }))
      .sort((a, b) => a.name.localeCompare(b.name, "fr"));
  }, [rows]);

  const filtres = useMemo(() => {
    let list = rows || [];
    if (fImmeuble) {
      list = list.filter((r) => String(r.immeuble_id) === fImmeuble);
    }
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter((r) =>
        `${r.locataire_nom || ""} ${r.prochain_locataire_nom || ""} ${r.immeuble_name} ${r.logement_numero}`
          .toLowerCase()
          .includes(q)
      );
    }
    // Rouges (résiliation en cours) en premier, puis sans bail, puis le
    // reste — ordre backend conservé (tri stable).
    return [...list].sort(
      (a, b) =>
        Number(b.resiliation_en_cours) - Number(a.resiliation_en_cours) ||
        Number(a.bail_id != null) - Number(b.bail_id != null)
    );
  }, [rows, fImmeuble, search]);

  const nbSansBail = (rows || []).filter((r) => r.bail_id == null).length;
  const nbADocumenter = (rows || []).filter(
    (r) => r.bail_id != null && r.document_id == null
  ).length;
  const nbResiliations = (rows || []).filter(
    (r) => r.resiliation_en_cours
  ).length;

  return (
    <>
      <ImmobilierTopbar
        breadcrumbs={[
          { label: "Gestion immobilière", href: "/immobilier" },
          { label: "Baux" }
        ]}
      />
      <div className="space-y-4 p-4 sm:p-6">
        {/* En-tête au MÊME format que les autres pages du pôle
            (Paiements, Suivis annuels) : pastille + titre + sous-titre,
            puis une rangée de tuiles de synthèse. */}
        <header className="flex items-start gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent-500/15 text-accent-500">
            <ClipboardList className="h-5 w-5" />
          </span>
          <div>
            <h1 className="text-2xl font-bold text-white">Baux</h1>
            <p className="mt-1 max-w-2xl text-sm text-white/60">
              Une ligne par logement : créer le bail, importer le PDF
              signé (il devient actif) et mettre fin au bail. La collecte
              des loyers, elle, se fait sur la page Paiements.
            </p>
          </div>
        </header>

        {/* Tuiles de synthèse — les mêmes compteurs que le tableau. */}
        {rows ? (
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatTile
              label="Logements"
              value={String(rows.length)}
              sub="au portefeuille"
            />
            <StatTile
              label="Sans bail"
              value={String(nbSansBail)}
              sub={nbSansBail > 0 ? "à créer 👇" : "tous couverts"}
            />
            <StatTile
              label="PDF à importer"
              value={String(nbADocumenter)}
              sub="bail actif, document manquant"
              tone={nbADocumenter > 0 ? "amber" : undefined}
            />
            <StatTile
              label="Résiliations"
              value={String(nbResiliations)}
              sub={
                nbResiliations > 0
                  ? "signature attendue"
                  : "rien à signaler"
              }
              tone={nbResiliations > 0 ? "rose" : undefined}
            />
          </div>
        ) : null}

        {/* MÊME bandeau que la page Paiements (composant partagé),
            filtré sur l'immeuble quand ?immeuble_id= est présent. */}
        <BandeauAvisRenouvellement immeubleId={immeubleId} />
        <BandeauBailManquant immeubleId={immeubleId} />
        <BandeauDepotARembourser immeubleId={immeubleId} />

        <div className="rounded-2xl border border-sky-400/30 bg-sky-500/10 p-4 text-xs text-sky-200">
          <p className="font-semibold text-white">Comment ça marche</p>
          <p className="mt-1">
            Le bail se prépare et se signe dans le système de la CORPIQ —
            ici vit le SUIVI, connecté au kanban Locations (changer le
            statut ici le change là-bas). Crée le bail, importe le PDF
            signé (il devient actif), et mets fin au bail : l&apos;entente
            de résiliation part pour signature en ligne (ligne ROUGE
            jusqu&apos;à la signature, puis résiliation et relocation
            automatiques) ou fin immédiate sans avis.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <select
            value={fImmeuble}
            onChange={(e) => setFImmeuble(e.target.value)}
            className="input w-auto text-sm"
          >
            <option value="">Tous les immeubles</option>
            {immeubles.map((i) => (
              <option key={i.id} value={String(i.id)}>
                {i.name}
              </option>
            ))}
          </select>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-white/40" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Locataire, immeuble, logement…"
              className="input w-56 pl-8 text-sm"
            />
          </div>
          {/* Les compteurs vivent maintenant dans les tuiles d'en-tête ;
              ici on n'affiche que le nombre de lignes FILTRÉES. */}
          {rows ? (
            <span className="text-xs text-white/50">
              {filtres.length} logement{filtres.length > 1 ? "s" : ""}{" "}
              affiché{filtres.length > 1 ? "s" : ""}
            </span>
          ) : null}
          {/* Export CSV/Excel — même filtre immeuble que le tableau. */}
          <span className="ml-auto">
            <BoutonExport
              cibles={[
                {
                  base: "/api/v1/immobilier/exports/baux",
                  sujet: "baux",
                  params: {
                    immeuble_id: fImmeuble || immeubleId || undefined
                  }
                }
              ]}
            />
          </span>
        </div>

        {flash ? (
          <p className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">
            {flash}
          </p>
        ) : null}
        {err ? (
          <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
            <AlertTriangle className="mr-1.5 inline h-3.5 w-3.5" />
            {err}
          </p>
        ) : null}

        {rows === null ? (
          <p className="flex items-center gap-2 text-xs text-white/50">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Chargement…
          </p>
        ) : filtres.length === 0 ? (
          <p className="rounded-lg border border-brand-800 bg-brand-900 px-4 py-3 text-sm text-white/60">
            Aucun logement ne correspond aux filtres.
          </p>
        ) : (
          <TableauSuiviBaux
            rows={filtres}
            onChanged={() => void load()}
            onFlash={setFlash}
          />
        )}

        <p className="text-[11px] text-white/40">
          Importer le PDF d&apos;un bail « proposé » le rend ACTIF et
          règle le dossier de relocation lié — partout dans Kratos.
        </p>
      </div>

    </>
  );
}

/** Tuile de synthèse — MÊME rendu que la page Paiements (`kpi-card`). */
function StatTile({
  label,
  value,
  sub,
  tone
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "emerald" | "amber" | "rose";
}) {
  return (
    <div className="kpi-card">
      <p className="text-[11px] uppercase tracking-wider text-white/50">
        {label}
      </p>
      <p
        className={`mt-1 text-xl font-bold tabular-nums ${
          tone === "emerald"
            ? "text-emerald-300"
            : tone === "amber"
              ? "text-amber-300"
              : tone === "rose"
                ? "text-rose-300"
                : "text-white"
        }`}
      >
        {value}
      </p>
      {sub ? <p className="mt-0.5 text-[10px] text-white/40">{sub}</p> : null}
    </div>
  );
}

