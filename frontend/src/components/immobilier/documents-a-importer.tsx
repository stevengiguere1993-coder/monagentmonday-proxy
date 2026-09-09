"use client";

/**
 * Zone « Documents » MULTI-FICHIERS partagée (retour Phil 2026-09-09) :
 * à la création d'un locataire — modale Locataires et création inline
 * depuis « Assigner un locataire » — le gestionnaire dépose PLUSIEURS
 * pièces d'un coup (bail, règlements de l'immeuble, assurance…), avec un
 * type par fichier. Le composant ne téléverse RIEN lui-même : il tient
 * la liste ; l'appelant enchaîne les imports avec `importerEnSerie`
 * (séquentiel, progression « 2/4 », erreurs par fichier sans bloquer
 * les autres).
 */

import { useRef, useState, type DragEvent } from "react";
import { FileText, Loader2, Plus, Upload, X } from "lucide-react";

import {
  IMM_DOC_TYPES,
  versFichiersAImporter,
  type FichierAImporter
} from "@/components/immobilier/doc-types";

export const ACCEPT_DOCS = "application/pdf,image/jpeg,image/png";

/** Résultat d'un import : `erreur` null = déposé. */
export type ImportResultat = {
  fichier: FichierAImporter;
  erreur: string | null;
};

/**
 * Enchaîne `envoyer` fichier par fichier (séquentiel — un seul upload à
 * la fois, l'API n'aime pas les rafales) et rapporte la progression.
 * Une erreur sur un fichier N'ARRÊTE PAS les suivants.
 */
export async function importerEnSerie(
  fichiers: readonly FichierAImporter[],
  envoyer: (f: FichierAImporter) => Promise<unknown>,
  onProgress?: (fait: number, total: number) => void
): Promise<ImportResultat[]> {
  const out: ImportResultat[] = [];
  let fait = 0;
  onProgress?.(0, fichiers.length);
  for (const f of fichiers) {
    try {
      await envoyer(f);
      out.push({ fichier: f, erreur: null });
    } catch (e) {
      out.push({
        fichier: f,
        erreur: (e as Error)?.message || "Import impossible."
      });
    }
    fait += 1;
    onProgress?.(fait, fichiers.length);
  }
  return out;
}

/** Texte « 2/4 » d'une progression d'import. */
export function texteProgression(
  p: { fait: number; total: number } | null | undefined
): string {
  if (!p || p.total === 0) return "";
  return `${Math.min(p.fait, p.total)}/${p.total}`;
}

function tailleLisible(octets: number): string {
  if (octets < 1024) return `${octets} o`;
  if (octets < 1024 * 1024) return `${Math.round(octets / 1024)} Ko`;
  return `${(octets / (1024 * 1024)).toFixed(1)} Mo`;
}

/**
 * Liste des fichiers choisis : un select de type par ligne + retrait.
 * `resultats` (après import) colore chaque ligne : déposé / erreur.
 */
export function FichiersAImporterListe({
  fichiers,
  onChange,
  disabled,
  resultats,
  avecType = true
}: {
  fichiers: FichierAImporter[];
  onChange: (fichiers: FichierAImporter[]) => void;
  disabled?: boolean;
  resultats?: ImportResultat[] | null;
  /** false = pas de choix du type (un seul fichier « autre »). */
  avecType?: boolean;
}) {
  if (fichiers.length === 0) return null;
  const resultatDe = (key: string) =>
    resultats?.find((r) => r.fichier.key === key) ?? null;
  return (
    <ul className="divide-y divide-brand-800 rounded-lg border border-brand-800">
      {fichiers.map((f) => {
        const res = resultatDe(f.key);
        return (
          <li
            key={f.key}
            className="flex flex-wrap items-center gap-2 px-2.5 py-1.5"
          >
            <FileText className="h-3.5 w-3.5 flex-shrink-0 text-white/40" />
            <span
              className="min-w-0 flex-1 truncate text-xs text-white"
              title={f.file.name}
            >
              {f.file.name}
              <span className="ml-1.5 text-[10px] text-white/40">
                {tailleLisible(f.file.size)}
              </span>
            </span>
            {avecType ? (
              <select
                value={f.type}
                disabled={disabled || Boolean(res && !res.erreur)}
                onChange={(e) =>
                  onChange(
                    fichiers.map((x) =>
                      x.key === f.key ? { ...x, type: e.target.value } : x
                    )
                  )
                }
                className="input w-auto py-1 text-xs disabled:opacity-60"
                aria-label={`Type du document ${f.file.name}`}
              >
                {IMM_DOC_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            ) : null}
            {res ? (
              res.erreur ? (
                <span
                  className="text-[11px] text-rose-300"
                  title={res.erreur}
                >
                  Échec : {res.erreur}
                </span>
              ) : (
                <span className="text-[11px] text-emerald-300">Déposé</span>
              )
            ) : null}
            {!res || res.erreur ? (
              <button
                type="button"
                disabled={disabled}
                onClick={() =>
                  onChange(fichiers.filter((x) => x.key !== f.key))
                }
                className="rounded p-1 text-white/40 hover:text-rose-300 disabled:opacity-40"
                title="Retirer ce fichier"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * Zone complète : glisser-déposer (ou bouton) + liste avec types +
 * progression pendant l'import. Optionnelle dans les formulaires : sans
 * fichier, rien ne change au flux existant.
 */
export function DocumentsAImporterZone({
  fichiers,
  onChange,
  disabled,
  progression,
  resultats,
  titre = "Documents",
  aide
}: {
  fichiers: FichierAImporter[];
  onChange: (fichiers: FichierAImporter[]) => void;
  disabled?: boolean;
  /** Import en cours : « 2/4 ». */
  progression?: { fait: number; total: number } | null;
  /** Résultats après import (colore les lignes). */
  resultats?: ImportResultat[] | null;
  titre?: string;
  /** Phrase d'aide sous le titre (ce que devient un fichier « Bail »). */
  aide?: string;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [survol, setSurvol] = useState(false);
  const enCours = Boolean(progression && progression.fait < progression.total);

  function ajouter(list: FileList | File[] | null | undefined) {
    if (!list || disabled) return;
    const nouveaux = versFichiersAImporter(Array.from(list), fichiers);
    if (nouveaux.length > 0) onChange([...fichiers, ...nouveaux]);
  }

  function onDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setSurvol(false);
    ajouter(e.dataTransfer?.files);
  }

  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <label className="label mb-0">{titre}</label>
        <span className="text-[11px] text-white/40">optionnel</span>
        {enCours ? (
          <span className="ml-auto inline-flex items-center gap-1 text-[11px] text-accent-500">
            <Loader2 className="h-3 w-3 animate-spin" />
            Import {texteProgression(progression)}
          </span>
        ) : null}
      </div>
      {aide ? <p className="mb-2 text-[11px] text-white/45">{aide}</p> : null}
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ACCEPT_DOCS}
        className="hidden"
        disabled={disabled}
        onChange={(e) => {
          ajouter(e.target.files);
          e.target.value = "";
        }}
      />
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled}
        onClick={() => !disabled && inputRef.current?.click()}
        onKeyDown={(e) => {
          if (disabled) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled && !survol) setSurvol(true);
        }}
        onDragLeave={() => setSurvol(false)}
        onDrop={onDrop}
        className={`flex cursor-pointer flex-col items-center justify-center gap-1 rounded-xl border border-dashed px-3 py-3 text-center text-xs transition ${
          survol
            ? "border-accent-500 bg-accent-500/10 text-white"
            : "border-brand-700 text-white/50 hover:border-brand-600 hover:text-white/70"
        } ${disabled ? "cursor-not-allowed opacity-50" : ""}`}
      >
        <Upload className="h-4 w-4" />
        <span>
          Glisse des fichiers ici ou{" "}
          <span className="font-semibold text-accent-500">
            <Plus className="inline h-3 w-3" /> choisis-les
          </span>
        </span>
        <span className="text-[10px] text-white/35">
          PDF, JPG ou PNG · 20 Mo max par fichier · plusieurs à la fois
        </span>
      </div>
      {fichiers.length > 0 ? (
        <div className="mt-2">
          <FichiersAImporterListe
            fichiers={fichiers}
            onChange={onChange}
            disabled={disabled}
            resultats={resultats}
          />
        </div>
      ) : null}
    </div>
  );
}
