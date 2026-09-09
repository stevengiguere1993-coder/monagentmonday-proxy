"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  FileText,
  Loader2,
  Pencil,
  Phone,
  Plus,
  Scale,
  Trash2,
  Users,
  X
} from "lucide-react";

import { Link } from "@/i18n/navigation";
import { authedFetch } from "@/lib/auth";
import {
  ImportDocButton,
  importDocument,
  type BailDocument
} from "@/components/immobilier/tal-avis";

/**
 * Dossier TAL simple + garants/contacts d'un locataire (retours Phil
 * 2026-09-09, points 5 et 8).
 *
 * - `TalDossiersSection` : fiche locataire — liste des dossiers de ses
 *   baux, édition en place (statut, motif, numéro, dates, notes) et
 *   pièces rattachées (import / ouverture PDF).
 * - `TalPastille` : « TAL » + nombre de dossiers en cours (en-têtes des
 *   fiches logement et immeuble).
 * - `ouvrirDossierTal` : bouton « Ouvrir un dossier TAL » des pages
 *   Paiements et du miroir fiche immeuble (crée un dossier non-paiement).
 * - `GarantsContactsSection` : fiche locataire — liste éditable.
 * - `GarantsContactsLecture` : fiche logement — lecture seule + lien.
 * - `normaliserTexte` : recherche accents-insensible (page Paiements).
 */

// ─── Types & libellés ────────────────────────────────────────────────

export type TalDossier = {
  id: number;
  bail_id: number;
  locataire_id: number | null;
  logement_id: number | null;
  immeuble_id: number | null;
  motif: string;
  statut: string;
  numero_dossier: string | null;
  ouvert_le: string | null;
  audience_le: string | null;
  decision_le: string | null;
  notes: string | null;
  created_by_email?: string | null;
  created_at?: string;
  updated_at?: string;
  locataire_name?: string | null;
  immeuble_name?: string | null;
  logement_numero?: string | null;
  nb_documents?: number;
};

export type TalDossierDetail = TalDossier & { documents: BailDocument[] };

export const TAL_MOTIFS: [string, string][] = [
  ["non_paiement", "Non-paiement"],
  ["retards", "Retards répétés"],
  ["reprise", "Reprise du logement"],
  ["travaux", "Travaux / évacuation"],
  ["non_reconduction", "Non-reconduction"],
  ["troubles", "Troubles de jouissance"],
  ["autre", "Autre"]
];

export const TAL_STATUTS: [string, string][] = [
  ["a_ouvrir", "À ouvrir"],
  ["ouvert", "Ouvert"],
  ["audience", "Audience fixée"],
  ["decision", "Décision rendue"],
  ["ferme", "Fermé"]
];

const TAL_STATUT_BADGE: Record<string, string> = {
  a_ouvrir: "badge-amber",
  ouvert: "badge-violet",
  audience: "badge-sky",
  decision: "badge-blue",
  ferme: "badge-neutral"
};

export function talMotifLabel(m: string): string {
  return TAL_MOTIFS.find(([v]) => v === m)?.[1] ?? m;
}

export function talStatutLabel(s: string): string {
  return TAL_STATUTS.find(([v]) => v === s)?.[1] ?? s;
}

export type LocataireContact = {
  id: number;
  locataire_id: number;
  role: string;
  full_name: string;
  email: string | null;
  phone: string | null;
  relation: string | null;
  paie_le_loyer: boolean;
  notes: string | null;
  actif: boolean;
};

export const CONTACT_ROLES: [string, string][] = [
  ["garant", "Garant"],
  ["colocataire", "Colocataire"],
  ["occupant", "Occupant"],
  ["urgence", "Contact d'urgence"]
];

export function roleLabel(r: string): string {
  return CONTACT_ROLES.find(([v]) => v === r)?.[1] ?? r;
}

/** Minuscules + sans accents : « Sébastien » matche « sebastien ». */
export function normaliserTexte(s: string | null | undefined): string {
  return (s || "")
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

const INPUT_CLS =
  "rounded-md border border-brand-800 bg-brand-950 px-2 py-1.5 text-xs text-white outline-none focus:border-accent-500";

//: Même événement que DocumentsSection (tal-avis.tsx) : un import de
//: pièce TAL rafraîchit aussi la section Documents de la fiche.
const DOCS_EVENT = "kratos:documents-changed";

async function lireErreur(r: Response): Promise<string> {
  const t = await r.text().catch(() => "");
  try {
    const j = JSON.parse(t) as { detail?: unknown };
    if (typeof j.detail === "string") return j.detail;
  } catch {
    /* texte brut */
  }
  return t.slice(0, 200) || `HTTP ${r.status}`;
}

// ─── Ouverture depuis Paiements / miroir immeuble ────────────────────

/** Crée un dossier TAL non-paiement (statut ouvert) sur ce bail. Le
 *  bouton des pages Paiements / fiche immeuble n'apparaît que quand il
 *  n'y a AUCUN dossier en cours, donc pas de doublon. */
export async function ouvrirDossierTal(bailId: number): Promise<TalDossier> {
  const r = await authedFetch(
    `/api/v1/immobilier/baux/${bailId}/tal-dossiers`,
    {
      method: "POST",
      body: JSON.stringify({ motif: "non_paiement", statut: "ouvert" })
    }
  );
  if (!r.ok) throw new Error(await lireErreur(r));
  return (await r.json()) as TalDossier;
}

// ─── Pastille « TAL » (en-têtes logement / immeuble) ─────────────────

export function TalPastille({
  immeubleId,
  logementId,
  locataireId
}: {
  immeubleId?: number;
  logementId?: number;
  locataireId?: number;
}) {
  const [dossiers, setDossiers] = useState<TalDossier[]>([]);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const qs = new URLSearchParams({ statut: "en_cours" });
      if (immeubleId != null) qs.set("immeuble_id", String(immeubleId));
      if (logementId != null) qs.set("logement_id", String(logementId));
      if (locataireId != null) qs.set("locataire_id", String(locataireId));
      try {
        const r = await authedFetch(
          `/api/v1/immobilier/tal-dossiers?${qs.toString()}`
        );
        if (r.ok && !cancelled) setDossiers((await r.json()) as TalDossier[]);
      } catch {
        /* pastille facultative */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [immeubleId, logementId, locataireId]);
  if (dossiers.length === 0) return null;
  // Un seul locataire concerné → la pastille mène à sa fiche.
  const cible = dossiers.length === 1 ? dossiers[0].locataire_id : null;
  const contenu = (
    <>
      <Scale className="h-3 w-3" /> TAL · {dossiers.length}
    </>
  );
  const title = `${dossiers.length} dossier${
    dossiers.length > 1 ? "s" : ""
  } TAL en cours`;
  if (cible != null) {
    return (
      <Link
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        href={`/immobilier/locataires/${cible}` as any}
        title={`${title} — ouvrir la fiche du locataire`}
        className="badge badge-violet inline-flex items-center gap-1 hover:underline"
      >
        {contenu}
      </Link>
    );
  }
  return (
    <span
      title={title}
      className="badge badge-violet inline-flex items-center gap-1"
    >
      {contenu}
    </span>
  );
}

// ─── Section « Dossier TAL » (fiche locataire) ───────────────────────

type Draft = {
  statut: string;
  motif: string;
  numero_dossier: string;
  ouvert_le: string;
  audience_le: string;
  decision_le: string;
  notes: string;
};

function draftDe(d: TalDossier): Draft {
  return {
    statut: d.statut,
    motif: d.motif,
    numero_dossier: d.numero_dossier ?? "",
    ouvert_le: d.ouvert_le ?? "",
    audience_le: d.audience_le ?? "",
    decision_le: d.decision_le ?? "",
    notes: d.notes ?? ""
  };
}

function draftEgal(a: Draft, b: Draft): boolean {
  return (Object.keys(a) as (keyof Draft)[]).every((k) => a[k] === b[k]);
}

export function TalDossiersSection({
  locataireId,
  baux
}: {
  locataireId: number;
  /** Baux du locataire (pour « Ouvrir un dossier ») — libellé affiché. */
  baux: { id: number; label: string; status?: string }[];
}) {
  const [dossiers, setDossiers] = useState<TalDossier[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<number, Draft>>({});
  const [savingId, setSavingId] = useState<number | null>(null);
  const [docs, setDocs] = useState<Record<number, BailDocument[]>>({});
  const [ouvert, setOuvert] = useState<Record<number, boolean>>({});
  const [importingId, setImportingId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [bailChoisi, setBailChoisi] = useState<number | "">("");
  const [choixBail, setChoixBail] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/tal-dossiers?locataire_id=${locataireId}`
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const rows = (await r.json()) as TalDossier[];
      setDossiers(rows);
      setDrafts(Object.fromEntries(rows.map((d) => [d.id, draftDe(d)])));
    } catch (e) {
      setErr(`Dossiers TAL : ${(e as Error).message}`);
      setDossiers([]);
    }
  }, [locataireId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function loadDocs(dossierId: number) {
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/tal-dossiers/${dossierId}`
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = (await r.json()) as TalDossierDetail;
      setDocs((m) => ({ ...m, [dossierId]: d.documents }));
    } catch (e) {
      setErr(`Pièces : ${(e as Error).message}`);
    }
  }

  function toggleDocs(dossierId: number) {
    const next = !ouvert[dossierId];
    setOuvert((m) => ({ ...m, [dossierId]: next }));
    if (next && !docs[dossierId]) void loadDocs(dossierId);
  }

  async function save(d: TalDossier) {
    const draft = drafts[d.id];
    if (!draft) return;
    setSavingId(d.id);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/tal-dossiers/${d.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            statut: draft.statut,
            motif: draft.motif,
            numero_dossier: draft.numero_dossier.trim() || null,
            ouvert_le: draft.ouvert_le || null,
            audience_le: draft.audience_le || null,
            decision_le: draft.decision_le || null,
            notes: draft.notes.trim() || null
          })
        }
      );
      if (!r.ok) throw new Error(await lireErreur(r));
      await load();
    } catch (e) {
      setErr(`Enregistrement : ${(e as Error).message}`);
    } finally {
      setSavingId(null);
    }
  }

  async function creer(bailId: number) {
    setCreating(true);
    setErr(null);
    try {
      await ouvrirDossierTal(bailId);
      setChoixBail(false);
      await load();
    } catch (e) {
      setErr(`Ouverture : ${(e as Error).message}`);
    } finally {
      setCreating(false);
    }
  }

  function onOuvrir() {
    if (baux.length === 1) {
      void creer(baux[0].id);
      return;
    }
    setChoixBail(true);
  }

  async function importer(dossierId: number, file: File) {
    setImportingId(dossierId);
    setErr(null);
    try {
      await importDocument({ file, type: "tal_piece", talDossierId: dossierId });
      await loadDocs(dossierId);
      setOuvert((m) => ({ ...m, [dossierId]: true }));
      window.dispatchEvent(new Event(DOCS_EVENT));
      await load();
    } catch (e) {
      setErr(`Import : ${(e as Error).message}`);
    } finally {
      setImportingId(null);
    }
  }

  async function ouvrirPdf(docId: number) {
    try {
      const r = await authedFetch(`/api/v1/immobilier/documents/${docId}/pdf`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const url = URL.createObjectURL(await r.blob());
      window.open(url, "_blank");
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (e) {
      setErr(`Ouverture : ${(e as Error).message}`);
    }
  }

  const bauxActifs = baux.filter((b) => !b.status || b.status === "actif");
  const bauxPourOuvrir = bauxActifs.length > 0 ? bauxActifs : baux;

  return (
    <section className="rounded-2xl border border-brand-800 bg-brand-900 p-5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="inline-flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-accent-500">
          <Scale className="h-4 w-4" /> Dossier TAL
        </h2>
        {choixBail ? (
          <div className="flex items-center gap-2">
            <select
              value={bailChoisi}
              onChange={(e) =>
                setBailChoisi(e.target.value ? Number(e.target.value) : "")
              }
              className={INPUT_CLS}
            >
              <option value="">Quel bail ?</option>
              {bauxPourOuvrir.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={bailChoisi === "" || creating}
              onClick={() => bailChoisi !== "" && void creer(bailChoisi)}
              className="btn-accent btn-xs disabled:opacity-60"
            >
              {creating ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Check className="h-3.5 w-3.5" />
              )}
              Ouvrir
            </button>
            <button
              type="button"
              onClick={() => setChoixBail(false)}
              className="btn-secondary btn-xs"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={onOuvrir}
            disabled={creating || bauxPourOuvrir.length === 0}
            title={
              bauxPourOuvrir.length === 0
                ? "Aucun bail : rien à porter au TAL"
                : "Ouvrir un dossier (non-paiement par défaut — modifiable ensuite)"
            }
            className="btn-secondary btn-xs disabled:opacity-60"
          >
            {creating ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Plus className="h-3.5 w-3.5" />
            )}
            Ouvrir un dossier TAL
          </button>
        )}
      </div>

      {err ? (
        <p className="mb-2 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          <AlertTriangle className="mr-1.5 inline h-3.5 w-3.5" />
          {err}
        </p>
      ) : null}

      {dossiers === null ? (
        <p className="text-xs text-white/50">Chargement…</p>
      ) : dossiers.length === 0 ? (
        <p className="text-sm text-white/50">
          Aucun dossier au Tribunal administratif du logement pour ce
          locataire. « Ouvrir un dossier TAL » (ici, sur la page Paiements
          ou depuis la fiche immeuble) le rend visible à toute
          l&apos;équipe.
        </p>
      ) : (
        <div className="space-y-3">
          {dossiers.map((d) => {
            const draft = drafts[d.id] ?? draftDe(d);
            const dirty = !draftEgal(draft, draftDe(d));
            const set = (k: keyof Draft, v: string) =>
              setDrafts((m) => ({ ...m, [d.id]: { ...draft, [k]: v } }));
            const pieces = docs[d.id];
            return (
              <div
                key={d.id}
                className="rounded-lg border border-brand-800 bg-brand-950/60 p-3"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className={`badge ${
                      TAL_STATUT_BADGE[draft.statut] || "badge-neutral"
                    }`}
                  >
                    {talStatutLabel(draft.statut)}
                  </span>
                  <span className="text-sm font-medium text-white">
                    {talMotifLabel(draft.motif)}
                  </span>
                  <span className="text-xs text-white/60">
                    {d.immeuble_name}
                    {d.logement_numero ? ` · ${d.logement_numero}` : ""}
                  </span>
                  {d.numero_dossier ? (
                    <span className="font-mono text-xs text-white/60">
                      nº {d.numero_dossier}
                    </span>
                  ) : null}
                  <button
                    type="button"
                    onClick={() => toggleDocs(d.id)}
                    className="ml-auto inline-flex items-center gap-1 text-xs text-accent-500 hover:underline"
                    title="Pièces rattachées (mise en demeure, avis d'audience, décision…)"
                  >
                    <FileText className="h-3.5 w-3.5" />
                    Pièces ({pieces ? pieces.length : d.nb_documents ?? 0})
                  </button>
                </div>

                <div className="mt-2 grid gap-2 sm:grid-cols-3">
                  <label className="text-[11px] text-white/60">
                    Statut
                    <select
                      value={draft.statut}
                      onChange={(e) => set("statut", e.target.value)}
                      className={`${INPUT_CLS} mt-0.5 w-full`}
                    >
                      {TAL_STATUTS.map(([v, l]) => (
                        <option key={v} value={v}>
                          {l}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="text-[11px] text-white/60">
                    Motif
                    <select
                      value={draft.motif}
                      onChange={(e) => set("motif", e.target.value)}
                      className={`${INPUT_CLS} mt-0.5 w-full`}
                    >
                      {TAL_MOTIFS.map(([v, l]) => (
                        <option key={v} value={v}>
                          {l}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="text-[11px] text-white/60">
                    Nº de dossier
                    <input
                      value={draft.numero_dossier}
                      onChange={(e) => set("numero_dossier", e.target.value)}
                      placeholder="ex. 123456 31 20260901 G"
                      className={`${INPUT_CLS} mt-0.5 w-full font-mono`}
                    />
                  </label>
                  <label className="text-[11px] text-white/60">
                    Ouvert le
                    <input
                      type="date"
                      value={draft.ouvert_le}
                      onChange={(e) => set("ouvert_le", e.target.value)}
                      className={`${INPUT_CLS} mt-0.5 w-full`}
                    />
                  </label>
                  <label className="text-[11px] text-white/60">
                    Audience le
                    <input
                      type="date"
                      value={draft.audience_le}
                      onChange={(e) => set("audience_le", e.target.value)}
                      className={`${INPUT_CLS} mt-0.5 w-full`}
                    />
                  </label>
                  <label className="text-[11px] text-white/60">
                    Décision le
                    <input
                      type="date"
                      value={draft.decision_le}
                      onChange={(e) => set("decision_le", e.target.value)}
                      className={`${INPUT_CLS} mt-0.5 w-full`}
                    />
                  </label>
                  <label className="text-[11px] text-white/60 sm:col-span-3">
                    Notes
                    <textarea
                      rows={2}
                      value={draft.notes}
                      onChange={(e) => set("notes", e.target.value)}
                      placeholder="Ce que l'équipe doit savoir (audience, entente, huissier…)"
                      className={`${INPUT_CLS} mt-0.5 w-full resize-y`}
                    />
                  </label>
                </div>
                {dirty ? (
                  <div className="mt-2 flex justify-end gap-2">
                    <button
                      type="button"
                      onClick={() =>
                        setDrafts((m) => ({ ...m, [d.id]: draftDe(d) }))
                      }
                      className="btn-secondary btn-xs"
                    >
                      Annuler
                    </button>
                    <button
                      type="button"
                      onClick={() => void save(d)}
                      disabled={savingId === d.id}
                      className="btn-accent btn-xs disabled:opacity-60"
                    >
                      {savingId === d.id ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Check className="h-3.5 w-3.5" />
                      )}
                      Enregistrer
                    </button>
                  </div>
                ) : null}

                {ouvert[d.id] ? (
                  <div className="mt-3 border-t border-brand-800 pt-2">
                    <div className="mb-1.5 flex items-center justify-between gap-2">
                      <p className="text-[10px] font-semibold uppercase tracking-wider text-white/50">
                        Pièces du dossier
                      </p>
                      <ImportDocButton
                        label="Importer"
                        busy={importingId === d.id}
                        onPick={(f) => void importer(d.id, f)}
                        title="Rattacher un PDF/JPG/PNG à ce dossier — il reste aussi dans les Documents du locataire"
                      />
                    </div>
                    {!pieces ? (
                      <p className="text-xs text-white/50">Chargement…</p>
                    ) : pieces.length === 0 ? (
                      <p className="text-xs text-white/50">
                        Aucune pièce — importe la mise en demeure, l&apos;avis
                        d&apos;audience ou la décision.
                      </p>
                    ) : (
                      <ul className="space-y-1">
                        {pieces.map((p) => (
                          <li key={p.id}>
                            <button
                              type="button"
                              onClick={() => void ouvrirPdf(p.id)}
                              className="inline-flex items-center gap-1.5 text-xs text-accent-500 hover:underline"
                            >
                              <FileText className="h-3.5 w-3.5" />
                              {p.titre}
                              {p.created_at ? (
                                <span className="text-white/45">
                                  · {p.created_at.slice(0, 10)}
                                </span>
                              ) : null}
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

// ─── Garants & contacts (fiche locataire, éditable) ──────────────────

type ContactForm = {
  role: string;
  full_name: string;
  phone: string;
  email: string;
  relation: string;
  paie_le_loyer: boolean;
};

const FORM_VIDE: ContactForm = {
  role: "garant",
  full_name: "",
  phone: "",
  email: "",
  relation: "",
  paie_le_loyer: false
};

function formDe(c: LocataireContact): ContactForm {
  return {
    role: c.role,
    full_name: c.full_name,
    phone: c.phone ?? "",
    email: c.email ?? "",
    relation: c.relation ?? "",
    paie_le_loyer: c.paie_le_loyer
  };
}

function ContactFormFields({
  form,
  onChange
}: {
  form: ContactForm;
  onChange: (f: ContactForm) => void;
}) {
  const set = <K extends keyof ContactForm>(k: K, v: ContactForm[K]) =>
    onChange({ ...form, [k]: v });
  return (
    <div className="grid gap-2 sm:grid-cols-3">
      <label className="text-[11px] text-white/60">
        Rôle
        <select
          value={form.role}
          onChange={(e) => set("role", e.target.value)}
          className={`${INPUT_CLS} mt-0.5 w-full`}
        >
          {CONTACT_ROLES.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
      </label>
      <label className="text-[11px] text-white/60 sm:col-span-2">
        Nom *
        <input
          required
          value={form.full_name}
          onChange={(e) => set("full_name", e.target.value)}
          placeholder="ex. Jacques Roy"
          className={`${INPUT_CLS} mt-0.5 w-full`}
        />
      </label>
      <label className="text-[11px] text-white/60">
        Téléphone
        <input
          value={form.phone}
          onChange={(e) => set("phone", e.target.value)}
          placeholder="514 555-1234"
          className={`${INPUT_CLS} mt-0.5 w-full`}
        />
      </label>
      <label className="text-[11px] text-white/60">
        Courriel
        <input
          type="email"
          value={form.email}
          onChange={(e) => set("email", e.target.value)}
          className={`${INPUT_CLS} mt-0.5 w-full`}
        />
      </label>
      <label className="text-[11px] text-white/60">
        Relation
        <input
          value={form.relation}
          onChange={(e) => set("relation", e.target.value)}
          placeholder="père, conjointe…"
          className={`${INPUT_CLS} mt-0.5 w-full`}
        />
      </label>
      <label className="inline-flex items-center gap-2 text-xs text-white/80 sm:col-span-3">
        <input
          type="checkbox"
          checked={form.paie_le_loyer}
          onChange={(e) => set("paie_le_loyer", e.target.checked)}
        />
        Paie le loyer (les virements arrivent à son nom)
      </label>
    </div>
  );
}

function PayeurBadge() {
  return (
    <span
      className="badge badge-emerald"
      title="Les virements de loyer arrivent au nom de cette personne"
    >
      paie le loyer
    </span>
  );
}

export function GarantsContactsSection({
  locataireId
}: {
  locataireId: number;
}) {
  const [contacts, setContacts] = useState<LocataireContact[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState<ContactForm>(FORM_VIDE);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState<ContactForm>(FORM_VIDE);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/locataires/${locataireId}/contacts`
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setContacts((await r.json()) as LocataireContact[]);
    } catch (e) {
      setErr(`Contacts : ${(e as Error).message}`);
      setContacts([]);
    }
  }, [locataireId]);

  useEffect(() => {
    void load();
  }, [load]);

  function corps(f: ContactForm) {
    return JSON.stringify({
      role: f.role,
      full_name: f.full_name.trim(),
      phone: f.phone.trim() || null,
      email: f.email.trim() || null,
      relation: f.relation.trim() || null,
      paie_le_loyer: f.paie_le_loyer
    });
  }

  async function ajouter() {
    if (!form.full_name.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/locataires/${locataireId}/contacts`,
        { method: "POST", body: corps(form) }
      );
      if (!r.ok) throw new Error(await lireErreur(r));
      setForm(FORM_VIDE);
      setAdding(false);
      await load();
    } catch (e) {
      setErr(`Ajout : ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function enregistrer(id: number) {
    if (!editForm.full_name.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/locataire-contacts/${id}`,
        { method: "PATCH", body: corps(editForm) }
      );
      if (!r.ok) throw new Error(await lireErreur(r));
      setEditingId(null);
      await load();
    } catch (e) {
      setErr(`Modification : ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function retirer(c: LocataireContact) {
    if (!window.confirm(`Retirer ${c.full_name} des contacts de ce locataire ?`))
      return;
    setBusy(true);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/locataire-contacts/${c.id}`,
        { method: "DELETE" }
      );
      if (!r.ok && r.status !== 204) throw new Error(await lireErreur(r));
      await load();
    } catch (e) {
      setErr(`Retrait : ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-2xl border border-brand-800 bg-brand-900 p-5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="inline-flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-accent-500">
          <Users className="h-4 w-4" /> Garants &amp; contacts
        </h2>
        {!adding ? (
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="btn-secondary btn-xs"
            title="Garant, colocataire, occupant ou contact d'urgence — sans créer de fiche"
          >
            <Plus className="h-3.5 w-3.5" /> Ajouter
          </button>
        ) : null}
      </div>

      {err ? (
        <p className="mb-2 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          <AlertTriangle className="mr-1.5 inline h-3.5 w-3.5" />
          {err}
        </p>
      ) : null}

      {adding ? (
        <div className="mb-3 rounded-lg border border-accent-500/40 bg-brand-950/60 p-3">
          <ContactFormFields form={form} onChange={setForm} />
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setAdding(false);
                setForm(FORM_VIDE);
              }}
              className="btn-secondary btn-xs"
            >
              Annuler
            </button>
            <button
              type="button"
              onClick={() => void ajouter()}
              disabled={busy || !form.full_name.trim()}
              className="btn-accent btn-xs disabled:opacity-60"
            >
              {busy ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Check className="h-3.5 w-3.5" />
              )}
              Ajouter
            </button>
          </div>
        </div>
      ) : null}

      {contacts === null ? (
        <p className="text-xs text-white/50">Chargement…</p>
      ) : contacts.length === 0 && !adding ? (
        <p className="text-sm text-white/50">
          Aucun garant ni contact. Ajoute ici la personne qui paie le
          loyer, un colocataire ou un contact d&apos;urgence : la recherche
          les trouve (chercher « Jacques » remonte ce locataire).
        </p>
      ) : (
        <div className="space-y-1.5">
          {contacts.map((c) =>
            editingId === c.id ? (
              <div
                key={c.id}
                className="rounded-lg border border-accent-500/40 bg-brand-950/60 p-3"
              >
                <ContactFormFields form={editForm} onChange={setEditForm} />
                <div className="mt-2 flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => setEditingId(null)}
                    className="btn-secondary btn-xs"
                  >
                    Annuler
                  </button>
                  <button
                    type="button"
                    onClick={() => void enregistrer(c.id)}
                    disabled={busy || !editForm.full_name.trim()}
                    className="btn-accent btn-xs disabled:opacity-60"
                  >
                    {busy ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Check className="h-3.5 w-3.5" />
                    )}
                    Enregistrer
                  </button>
                </div>
              </div>
            ) : (
              <div
                key={c.id}
                className="flex flex-wrap items-center gap-2 rounded-lg border border-brand-800 bg-brand-950/60 px-3 py-2 text-sm"
              >
                <span className="badge badge-neutral">{roleLabel(c.role)}</span>
                <span className="font-medium text-white">{c.full_name}</span>
                {c.relation ? (
                  <span className="text-xs text-white/60">({c.relation})</span>
                ) : null}
                {c.phone ? (
                  <a
                    href={`tel:${c.phone}`}
                    className="inline-flex items-center gap-1 text-xs text-accent-500 hover:underline"
                  >
                    <Phone className="h-3 w-3" />
                    {c.phone}
                  </a>
                ) : null}
                {c.email ? (
                  <a
                    href={`mailto:${c.email}`}
                    className="text-xs text-accent-500 hover:underline"
                  >
                    {c.email}
                  </a>
                ) : null}
                {c.paie_le_loyer ? <PayeurBadge /> : null}
                <span className="ml-auto inline-flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => {
                      setEditingId(c.id);
                      setEditForm(formDe(c));
                    }}
                    className="rounded p-1 text-white/60 hover:bg-white/10 hover:text-white"
                    title="Modifier"
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => void retirer(c)}
                    disabled={busy}
                    className="rounded p-1 text-white/60 hover:bg-rose-500/15 hover:text-rose-300"
                    title="Retirer"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </span>
              </div>
            )
          )}
        </div>
      )}
    </section>
  );
}

// ─── Lecture seule (fiche logement) ──────────────────────────────────

export function GarantsContactsLecture({
  locataireId
}: {
  locataireId: number;
}) {
  const [contacts, setContacts] = useState<LocataireContact[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const r = await authedFetch(
          `/api/v1/immobilier/locataires/${locataireId}/contacts`
        );
        if (!cancelled)
          setContacts(r.ok ? ((await r.json()) as LocataireContact[]) : []);
      } catch {
        if (!cancelled) setContacts([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [locataireId]);

  if (!contacts || contacts.length === 0) return null;
  return (
    <div className="rounded-lg border border-brand-800 bg-brand-950/60 px-3 py-2">
      <p className="mb-1 inline-flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-white/50">
        <Users className="h-3 w-3" /> Garants &amp; contacts
      </p>
      <ul className="space-y-1 text-xs">
        {contacts.map((c) => (
          <li key={c.id} className="flex flex-wrap items-center gap-1.5">
            <span className="text-white/60">{roleLabel(c.role)} :</span>
            <span className="font-medium text-white">{c.full_name}</span>
            {c.phone ? (
              <a
                href={`tel:${c.phone}`}
                className="inline-flex items-center gap-1 text-accent-500 hover:underline"
              >
                <Phone className="h-3 w-3" />
                {c.phone}
              </a>
            ) : null}
            {c.paie_le_loyer ? <PayeurBadge /> : null}
          </li>
        ))}
      </ul>
      <Link
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        href={`/immobilier/locataires/${locataireId}` as any}
        className="mt-1 inline-block text-[11px] text-accent-500 hover:underline"
      >
        Modifier dans la fiche du locataire →
      </Link>
    </div>
  );
}
