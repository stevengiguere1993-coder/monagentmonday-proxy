"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  DoorOpen,
  Loader2,
  Trash2,
  User,
  X
} from "lucide-react";

import { Link } from "@/i18n/navigation";
import { authedFetch } from "@/lib/auth";
import {
  echeanceLabel,
  LOUER_INDEFINIMENT_INFO,
  LOUER_INDEFINIMENT_LABEL,
  LouerIndefinimentBulle
} from "@/components/immobilier/fin-bail";

/**
 * Fiche logement partagée — modale d'affichage/édition d'UN logement.
 *
 * Utilisée par la page globale « Logements » et par l'onglet Logements
 * de la fiche immeuble. En mode création (logement = null), POST ; en
 * mode édition, PATCH. La section « Occupation » montre le bail ACTIF
 * du logement (si la liste des baux est fournie via props) avec un
 * lien vers la fiche du locataire.
 */

export type LogementFicheData = {
  id: number;
  immeuble_id: number;
  numero: string;
  nb_pieces_decimal?: number | null;
  nb_chambres?: number | null;
  nb_sdb?: number | null;
  superficie_pi2?: number | null;
  location_en_chambres?: boolean;
  etage?: number | null;
  type: string;
  status: string;
  loyer_demande?: number | null;
  /** Loyer RÉEL du bail actif (lecture seule, fourni par l'API liste).
   *  Un logement OCCUPÉ affiche ce loyer — le « loyer demandé » ne
   *  vaut que pour la relocation (vacant). */
  loyer_actuel?: number | null;
  /** Date à laquelle le logement se libère, quand un départ est ACTÉ.
   *  « Occupé » et « occupé mais libre le 31 août » ne sont pas le même
   *  état : le second demande de préparer la relocation. Une simple fin
   *  de bail ne remplit PAS ce champ — au Québec un bail se reconduit
   *  tacitement, échéance n'est pas départ. */
  libre_le?: string | null;
  notes?: string | null;
  /** Gestion externe : nom du locataire (facultatif). */
  locataire_externe_nom?: string | null;
};

export type LogementFicheBail = {
  id: number;
  logement_id: number;
  locataire_id: number;
  date_debut: string;
  date_fin: string;
  loyer_mensuel: number;
  status: string;
  /** Jour du mois où le loyer est payable (bail TAL « Ou le ___ »). */
  jour_echeance?: number | null;
};

/** 3.5 → « 3½ », 4 → « 4 » (convention QC). */
export function fmtPieces(n: number | null | undefined): string {
  if (n == null) return "—";
  return `${n}`.replace(".5", "½");
}

const STATUTS: [string, string][] = [
  ["vacant", "Vacant"],
  ["occupe", "Occupé"],
  ["reserve", "Réservé"],
  ["hors_location", "Hors location"]
];

const TYPES: [string, string][] = [
  ["residentiel", "Résidentiel"],
  ["commercial", "Commercial"],
  ["stationnement", "Stationnement"],
  ["rangement", "Rangement"],
  ["autre", "Autre"]
];

function fmtMoney(n: number | null | undefined): string {
  if (n == null) return "—";
  return new Intl.NumberFormat("fr-CA", {
    style: "currency",
    currency: "CAD",
    maximumFractionDigits: 0
  }).format(n);
}

function numOrNull(v: string): number | null {
  const t = v.trim();
  if (t === "") return null;
  const n = Number(t);
  return Number.isNaN(n) ? null : n;
}

export function LogementFiche({
  logement,
  immeubleId,
  bails,
  onClose,
  onSaved,
  onDeleted
}: {
  /** null = mode création (immeubleId requis). */
  logement: LogementFicheData | null;
  /** Requis en mode création. */
  immeubleId?: number;
  /** Baux de l'immeuble (pour la section Occupation + garde-fou suppression). */
  bails?: LogementFicheBail[];
  onClose: () => void;
  onSaved: (l: LogementFicheData) => void;
  onDeleted?: (id: number) => void;
}) {
  const isCreate = logement === null;
  const [form, setForm] = useState({
    numero: logement?.numero ?? "",
    type: logement?.type ?? "residentiel",
    status: logement?.status ?? "vacant",
    nb_pieces_decimal:
      logement?.nb_pieces_decimal != null
        ? String(logement.nb_pieces_decimal)
        : "",
    nb_chambres:
      logement?.nb_chambres != null ? String(logement.nb_chambres) : "",
    nb_sdb: logement?.nb_sdb != null ? String(logement.nb_sdb) : "",
    superficie_pi2:
      logement?.superficie_pi2 != null
        ? String(logement.superficie_pi2)
        : "",
    etage: logement?.etage != null ? String(logement.etage) : "",
    loyer_demande:
      logement?.loyer_demande != null ? String(logement.loyer_demande) : "",
    notes: logement?.notes ?? ""
  });
  const [enChambres, setEnChambres] = useState(
    !!logement?.location_en_chambres
  );
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [locataireName, setLocataireName] = useState<string | null>(null);

  // Baux de CE logement (le prop contient souvent tous les baux de l'immeuble).
  const bauxLogement = (bails || []).filter(
    (b) => logement != null && b.logement_id === logement.id
  );
  const bailActif =
    bauxLogement.find((b) => b.status === "actif") ||
    bauxLogement.find((b) => b.status === "propose") ||
    null;
  const hasLinkedData = bauxLogement.length > 0;

  // Nom du locataire du bail actif (le BailRead ne porte que l'id).
  useEffect(() => {
    if (!bailActif) return;
    let cancelled = false;
    void (async () => {
      try {
        const r = await authedFetch(
          `/api/v1/immobilier/locataires/${bailActif.locataire_id}`
        );
        if (!r.ok) return;
        const d = (await r.json()) as { full_name?: string };
        if (!cancelled) setLocataireName(d.full_name || null);
      } catch {
        /* silencieux — le lien reste utilisable */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [bailActif?.locataire_id]); // eslint-disable-line react-hooks/exhaustive-deps

  function set<K extends keyof typeof form>(k: K, v: string) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.numero.trim()) return;
    setSaving(true);
    setErr(null);
    try {
      const body: Record<string, unknown> = {
        numero: form.numero.trim(),
        type: form.type,
        status: form.status,
        nb_pieces_decimal: numOrNull(form.nb_pieces_decimal),
        nb_chambres: numOrNull(form.nb_chambres),
        nb_sdb: numOrNull(form.nb_sdb),
        superficie_pi2: numOrNull(form.superficie_pi2),
        location_en_chambres: enChambres,
        etage: numOrNull(form.etage),
        loyer_demande: numOrNull(form.loyer_demande),
        notes: form.notes.trim() ? form.notes : null
      };
      let res: Response;
      if (isCreate) {
        if (immeubleId == null) throw new Error("immeubleId manquant.");
        res = await authedFetch("/api/v1/immobilier/logements", {
          method: "POST",
          body: JSON.stringify({ ...body, immeuble_id: immeubleId })
        });
      } else {
        res = await authedFetch(
          `/api/v1/immobilier/logements/${logement.id}`,
          { method: "PATCH", body: JSON.stringify(body) }
        );
      }
      if (!res.ok) {
        const t = await res.text();
        throw new Error(t.slice(0, 240) || `HTTP ${res.status}`);
      }
      onSaved((await res.json()) as LogementFicheData);
    } catch (e2) {
      setErr((e2 as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (logement == null) return;
    setDeleting(true);
    setErr(null);
    try {
      const res = await authedFetch(
        `/api/v1/immobilier/logements/${logement.id}`,
        { method: "DELETE" }
      );
      if (!res.ok) {
        if (res.status === 409 || res.status === 400) {
          throw new Error(
            "Suppression impossible : des données (baux, paiements ou maintenance) sont liées à ce logement."
          );
        }
        if (res.status === 403) {
          throw new Error(
            "Tu n'as pas la permission de supprimer un logement."
          );
        }
        const t = await res.text();
        throw new Error(t.slice(0, 240) || `HTTP ${res.status}`);
      }
      onDeleted?.(logement.id);
      onClose();
    } catch (e2) {
      setErr((e2 as Error).message);
      setConfirmDelete(false);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm">
      <div className="my-8 w-full max-w-lg rounded-2xl border border-brand-800 bg-brand-950 shadow-2xl">
        <div className="flex items-center justify-between border-b border-brand-800 px-5 py-3">
          <h2 className="flex items-center gap-2 text-sm font-bold uppercase tracking-wider text-accent-500">
            <DoorOpen className="h-4 w-4" />
            {isCreate
              ? "Nouveau logement"
              : `Logement ${logement.numero}`}
          </h2>
          <button type="button" onClick={onClose} className="btn-ghost btn-xs">
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Occupation — bail actif du logement */}
        {!isCreate && bails ? (
          <div className="border-b border-brand-800 px-5 py-3">
            <p className="text-[10px] font-semibold uppercase tracking-wider text-white/50">
              Occupation
            </p>
            {bailActif ? (
              <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
                <Link
                  // eslint-disable-next-line @typescript-eslint/no-explicit-any
                  href={
                    `/immobilier/locataires/${bailActif.locataire_id}` as any
                  }
                  className="inline-flex items-center gap-1.5 font-medium text-accent-500 hover:underline"
                >
                  <User className="h-3.5 w-3.5" />
                  {locataireName || `Locataire #${bailActif.locataire_id}`}
                </Link>
                <span className="font-mono text-xs text-white/70">
                  {fmtMoney(bailActif.loyer_mensuel)}/mois
                </span>
                {/* Bail TAL « Ou le ___ » : muet quand c'est le 1er. */}
                {echeanceLabel(bailActif.jour_echeance) ? (
                  <span className="text-xs text-white/45">
                    {echeanceLabel(bailActif.jour_echeance)}
                  </span>
                ) : null}
                <span className="text-xs text-white/50">
                  {bailActif.date_debut} → {bailActif.date_fin}
                </span>
                {bailActif.status !== "actif" ? (
                  <span className="badge badge-sky">Proposé</span>
                ) : null}
              </div>
            ) : (
              <p className="mt-1.5 text-xs text-white/50">
                Aucun bail actif — logement libre.
                {bauxLogement.length > 0
                  ? ` (${bauxLogement.length} bail${bauxLogement.length > 1 ? "s" : ""} dans l'historique)`
                  : ""}
              </p>
            )}
          </div>
        ) : null}

        <form onSubmit={submit} className="grid gap-4 p-5">
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="label">Numéro *</label>
              <input
                required
                value={form.numero}
                onChange={(e) => set("numero", e.target.value)}
                className="input"
                placeholder="ex. 101"
              />
            </div>
            <div>
              <label className="label">Type</label>
              <select
                value={form.type}
                onChange={(e) => set("type", e.target.value)}
                className="input"
              >
                {TYPES.map(([v, l]) => (
                  <option key={v} value={v} className="bg-brand-950 text-white">
                    {l}
                  </option>
                ))}
                {!TYPES.some(([v]) => v === form.type) ? (
                  <option value={form.type} className="bg-brand-950 text-white">
                    {form.type}
                  </option>
                ) : null}
              </select>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <label className="label">Pièces</label>
              <input
                type="number"
                min={0}
                step={0.5}
                value={form.nb_pieces_decimal}
                onChange={(e) => set("nb_pieces_decimal", e.target.value)}
                className="input font-mono"
                placeholder="3.5"
              />
              {enChambres ? (
                <p className="mt-1 text-[11px] text-white/50">
                  Affiché : Chambre (louer indéfiniment)
                </p>
              ) : form.nb_pieces_decimal.trim() !== "" ? (
                <p className="mt-1 text-[11px] text-white/50">
                  Affiché : {fmtPieces(numOrNull(form.nb_pieces_decimal))}
                </p>
              ) : null}
            </div>
            <div>
              <label className="label">Chambres</label>
              <input
                type="number"
                min={0}
                step={1}
                value={form.nb_chambres}
                onChange={(e) => set("nb_chambres", e.target.value)}
                className="input font-mono"
              />
            </div>
            <div>
              <label className="label">Salles de bain</label>
              <input
                type="number"
                min={0}
                step={0.5}
                value={form.nb_sdb}
                onChange={(e) => set("nb_sdb", e.target.value)}
                className="input font-mono"
              />
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <label className="label">Superficie (pi²)</label>
              <input
                type="number"
                min={0}
                value={form.superficie_pi2}
                onChange={(e) => set("superficie_pi2", e.target.value)}
                className="input font-mono"
              />
              <label
                title={LOUER_INDEFINIMENT_INFO}
                className="mt-1.5 flex cursor-pointer items-center gap-2 text-xs text-white/70"
              >
                <input
                  type="checkbox"
                  checked={enChambres}
                  onChange={(e) => setEnChambres(e.target.checked)}
                  className="h-3.5 w-3.5 accent-accent-500"
                />
                {LOUER_INDEFINIMENT_LABEL}
              </label>
            </div>
            <div>
              <label className="label">Étage</label>
              <input
                type="number"
                step={1}
                value={form.etage}
                onChange={(e) => set("etage", e.target.value)}
                className="input font-mono"
              />
            </div>
            <div>
              <label className="label">Loyer demandé</label>
              <input
                type="number"
                min={0}
                value={form.loyer_demande}
                onChange={(e) => set("loyer_demande", e.target.value)}
                className="input font-mono"
                placeholder="$/mois"
              />
            </div>
          </div>

          {enChambres ? <LouerIndefinimentBulle /> : null}

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="label">Statut</label>
              <select
                value={form.status}
                onChange={(e) => set("status", e.target.value)}
                className="input"
              >
                {STATUTS.map(([v, l]) => (
                  <option key={v} value={v} className="bg-brand-950 text-white">
                    {l}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label className="label">Notes</label>
            <textarea
              rows={3}
              value={form.notes}
              onChange={(e) => set("notes", e.target.value)}
              className="input"
              placeholder="Particularités, travaux à prévoir…"
            />
          </div>

          {err ? (
            <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
              <AlertTriangle className="mr-1.5 inline h-3.5 w-3.5" />
              {err}
            </p>
          ) : null}

          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-brand-800 pt-4">
            {!isCreate ? (
              confirmDelete ? (
                <span className="inline-flex items-center gap-2 text-xs text-rose-300">
                  Supprimer définitivement ?
                  <button
                    type="button"
                    onClick={() => void remove()}
                    disabled={deleting}
                    className="btn-danger btn-xs disabled:opacity-50"
                  >
                    {deleting ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : (
                      <Trash2 className="h-3 w-3" />
                    )}
                    Oui, supprimer
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmDelete(false)}
                    disabled={deleting}
                    className="btn-ghost btn-xs"
                  >
                    Annuler
                  </button>
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => setConfirmDelete(true)}
                  disabled={hasLinkedData}
                  title={
                    hasLinkedData
                      ? "Impossible : des baux sont liés à ce logement."
                      : "Supprimer ce logement"
                  }
                  className="btn-outline-rose btn-xs disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <Trash2 className="h-3 w-3" /> Supprimer
                </button>
              )
            ) : (
              <span />
            )}
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={onClose}
                className="btn-secondary btn-sm"
              >
                Annuler
              </button>
              <button
                type="submit"
                disabled={saving || !form.numero.trim()}
                className="btn-accent btn-sm inline-flex items-center disabled:opacity-60"
              >
                {saving ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                ) : null}
                {isCreate ? "Créer" : "Enregistrer"}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
