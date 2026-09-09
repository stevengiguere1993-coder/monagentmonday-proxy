"use client";

/**
 * Boutons d'EXPORT du pôle Gestion locative (2026-09-09).
 *
 * - `BoutonExport` : menu « Exporter ▾ » → CSV / Excel pour une ou
 *   plusieurs cibles (page Paiements, Locataires, Baux, Logements,
 *   Immeubles, fiche immeuble). Option « Période » (du/au) pour les
 *   paiements, et entrée « Tout exporter (zip) » quand un zip existe.
 * - `BoutonExportZip` : bouton seul « Tout exporter (zip) » (section
 *   Documents, en-tête de la fiche locataire).
 *
 * Le téléchargement passe par `authedFetch` + blob + `URL.createObjectURL`
 * (comme l'ouverture d'un PDF conservé) : les <a href> ne portent pas le
 * jeton, donc pas de lien direct vers l'API.
 */

import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  Download,
  FileArchive,
  FileSpreadsheet,
  FileText,
  Loader2
} from "lucide-react";

import { authedFetch } from "@/lib/auth";

type ParamsExport = Record<string, string | number | boolean | null | undefined>;

export type CibleExport = {
  /** Libellé affiché quand il y a plusieurs cibles (« Paiements »…). */
  label?: string;
  /** Chemin API SANS `fmt`, ex. `/api/v1/immobilier/exports/locataires`. */
  base: string;
  /** Paramètres de requête (les vides sont ignorés). */
  params?: ParamsExport;
  /** Sert au nom de fichier de repli : kratos_<sujet>_<date>.<ext>. */
  sujet: string;
};

function construireUrl(base: string, params?: ParamsExport): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === "") continue;
    q.set(k, String(v));
  }
  const sep = base.includes("?") ? "&" : "?";
  const qs = q.toString();
  return qs ? `${base}${sep}${qs}` : base;
}

function nomDepuisEntetes(r: Response, repli: string): string {
  const cd = r.headers.get("content-disposition") || "";
  const m = /filename="([^"]+)"/.exec(cd);
  return m?.[1] || repli;
}

function dateDuJour(): string {
  return new Date().toISOString().slice(0, 10);
}

/** Télécharge un fichier protégé (blob → lien temporaire). Lève une
 *  Error avec le `detail` du backend (413 « trop de documents », 404
 *  « aucun document »…) pour que l'appelant l'affiche. */
export async function telechargerExport(
  path: string,
  nomRepli: string
): Promise<void> {
  const r = await authedFetch(path);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try {
      const j = (await r.json()) as { detail?: unknown };
      if (j && typeof j.detail === "string") msg = j.detail;
    } catch {
      /* corps non JSON : on garde le code HTTP */
    }
    throw new Error(msg);
  }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = nomDepuisEntetes(r, nomRepli);
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60000);
}

const ITEM_CLS =
  "flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-white/80 transition hover:bg-brand-900 hover:text-white disabled:opacity-50";

function BoutonTaille(size: "xs" | "sm"): string {
  return size === "xs" ? "btn-xs" : "btn-sm";
}

export function BoutonExport({
  cibles,
  zip,
  periode,
  size = "sm",
  variant = "secondary",
  label = "Exporter",
  className
}: {
  cibles: CibleExport[];
  /** Entrée « Tout exporter (zip) » en bas du menu. */
  zip?: { path: string; label?: string; sujet?: string };
  /** Section « Période » (du/au en YYYY-MM) — export paiements. */
  periode?: {
    base: string;
    sujet: string;
    /** Valeurs initiales (mois affiché par défaut). */
    du: string;
    au: string;
    params?: ParamsExport;
  };
  size?: "xs" | "sm";
  variant?: "secondary" | "outline";
  label?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [du, setDu] = useState(periode?.du || "");
  const [au, setAu] = useState(periode?.au || "");
  const ref = useRef<HTMLDivElement | null>(null);

  // Le mois affiché change → la période repart de là.
  useEffect(() => {
    if (periode) {
      setDu(periode.du);
      setAu(periode.au);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [periode?.du, periode?.au]);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
        setErr(null);
      }
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  async function lancer(cle: string, path: string, nomRepli: string) {
    setBusy(cle);
    setErr(null);
    try {
      await telechargerExport(path, nomRepli);
      setOpen(false);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  function exporter(c: CibleExport, fmt: "csv" | "xlsx", cle: string) {
    void lancer(
      cle,
      construireUrl(c.base, { ...(c.params || {}), fmt }),
      `kratos_${c.sujet}_${dateDuJour()}.${fmt}`
    );
  }

  const btnCls = `${variant === "outline" ? "btn-outline-accent" : "btn-secondary"} ${BoutonTaille(size)} ${className || ""}`;
  const iconCls = size === "xs" ? "h-3.5 w-3.5" : "h-4 w-4";
  const plusieurs = cibles.length > 1;

  return (
    <div ref={ref} className="relative inline-block">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={btnCls}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Exporter en CSV ou Excel"
      >
        <Download className={iconCls} />
        {label}
        <ChevronDown
          className={`${iconCls} transition ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open ? (
        <div className="absolute right-0 z-30 mt-1 w-64 rounded-lg border border-brand-700 bg-brand-950 py-1 shadow-2xl">
          {cibles.map((c, idx) => {
            const cleCsv = `${idx}-csv`;
            const cleXlsx = `${idx}-xlsx`;
            return (
              <div key={`${c.base}-${idx}`}>
                {plusieurs ? (
                  <div
                    className={`px-3 pb-0.5 text-[10px] font-semibold uppercase tracking-wider text-white/40 ${idx > 0 ? "mt-1 border-t border-brand-800 pt-1.5" : "pt-1"}`}
                  >
                    {c.label || c.sujet}
                  </div>
                ) : null}
                <button
                  type="button"
                  onClick={() => exporter(c, "csv", cleCsv)}
                  disabled={busy !== null}
                  className={ITEM_CLS}
                >
                  {busy === cleCsv ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <FileText className="h-3.5 w-3.5" />
                  )}
                  CSV (.csv)
                </button>
                <button
                  type="button"
                  onClick={() => exporter(c, "xlsx", cleXlsx)}
                  disabled={busy !== null}
                  className={ITEM_CLS}
                >
                  {busy === cleXlsx ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <FileSpreadsheet className="h-3.5 w-3.5" />
                  )}
                  Excel (.xlsx)
                </button>
              </div>
            );
          })}
          {periode ? (
            <div className="mt-1 border-t border-brand-800 pt-1.5">
              <div className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-wider text-white/40">
                Période
              </div>
              <div className="flex items-center gap-1 px-3 pb-1">
                <input
                  type="month"
                  value={du}
                  onChange={(e) => setDu(e.target.value)}
                  className="input w-full px-2 py-1 text-xs"
                  aria-label="Du (mois)"
                />
                <span className="text-[11px] text-white/40">→</span>
                <input
                  type="month"
                  value={au}
                  onChange={(e) => setAu(e.target.value)}
                  className="input w-full px-2 py-1 text-xs"
                  aria-label="Au (mois)"
                />
              </div>
              {(["csv", "xlsx"] as const).map((fmt) => {
                const cle = `periode-${fmt}`;
                const c: CibleExport = {
                  base: periode.base,
                  sujet: periode.sujet,
                  params: { ...(periode.params || {}), du, au }
                };
                return (
                  <button
                    key={cle}
                    type="button"
                    onClick={() => exporter(c, fmt, cle)}
                    disabled={busy !== null || !du || !au || du > au}
                    className={ITEM_CLS}
                    title={
                      du && au && du > au
                        ? "« Au » doit être après « Du »"
                        : undefined
                    }
                  >
                    {busy === cle ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : fmt === "csv" ? (
                      <FileText className="h-3.5 w-3.5" />
                    ) : (
                      <FileSpreadsheet className="h-3.5 w-3.5" />
                    )}
                    Période — {fmt === "csv" ? "CSV" : "Excel"}
                  </button>
                );
              })}
            </div>
          ) : null}
          {zip ? (
            <div className="mt-1 border-t border-brand-800 pt-1">
              <button
                type="button"
                onClick={() =>
                  void lancer(
                    "zip",
                    zip.path,
                    `kratos_documents_${zip.sujet || "export"}_${dateDuJour()}.zip`
                  )
                }
                disabled={busy !== null}
                className={ITEM_CLS}
                title="Tous les documents conservés (PDF) + index.csv"
              >
                {busy === "zip" ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <FileArchive className="h-3.5 w-3.5" />
                )}
                {zip.label || "Tout exporter (zip)"}
              </button>
            </div>
          ) : null}
          {err ? (
            <p className="mx-2 mt-1 rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-300">
              {err}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** Bouton seul « Tout exporter (zip) » — même style que ses voisins
 *  (« Importer » de la section Documents, « État de compte »). */
export function BoutonExportZip({
  path,
  sujet,
  label = "Tout exporter (zip)",
  size = "xs",
  variant = "secondary",
  title,
  className,
  onError
}: {
  path: string;
  /** Nom de fichier de repli. */
  sujet: string;
  label?: string;
  size?: "xs" | "sm";
  variant?: "secondary" | "outline";
  title?: string;
  className?: string;
  /** Sans `onError`, le message s'affiche sous le bouton. */
  onError?: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function go() {
    setBusy(true);
    setErr(null);
    try {
      await telechargerExport(
        path,
        `kratos_documents_${sujet}_${dateDuJour()}.zip`
      );
    } catch (e) {
      const msg = (e as Error).message;
      if (onError) onError(msg);
      else setErr(msg);
    } finally {
      setBusy(false);
    }
  }

  const btnCls = `${variant === "outline" ? "btn-outline-accent" : "btn-secondary"} ${BoutonTaille(size)} disabled:opacity-50 ${className || ""}`;
  const iconCls = size === "xs" ? "h-3.5 w-3.5" : "h-4 w-4";

  return (
    <span className="relative inline-flex flex-col items-start">
      <button
        type="button"
        onClick={() => void go()}
        disabled={busy}
        className={btnCls}
        title={title || "Télécharger tous les documents (PDF) dans un zip, avec un index.csv"}
      >
        {busy ? (
          <Loader2 className={`${iconCls} animate-spin`} />
        ) : (
          <FileArchive className={iconCls} />
        )}
        {label}
      </button>
      {err ? (
        <span className="absolute left-0 top-full z-30 mt-1 w-56 rounded-md border border-rose-500/40 bg-brand-950 px-2 py-1 text-[11px] text-rose-300 shadow-2xl">
          {err}
        </span>
      ) : null}
    </span>
  );
}
