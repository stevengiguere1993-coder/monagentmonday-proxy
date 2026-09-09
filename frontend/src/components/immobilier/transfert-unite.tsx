"use client";

/**
 * Transfert d'unité (points 11-12, retours Phil 2026-09-09) — un
 * locataire change de logement EN UN GESTE :
 * - son bail actuel se termine la veille du transfert ;
 * - un NOUVEAU bail (proposé, à signer) est créé sur la nouvelle unité ;
 * - le dépôt de garantie SUIT le locataire (par défaut) ;
 * - l'ancienne unité part en relocation, la nouvelle passe « bail en
 *   signature » au kanban.
 * Le bail signé (PDF du gestionnaire — ou bientôt notre système) se
 * joint dans la foulée et rend le nouveau bail actif.
 *
 * Même bouton partout où un bail actif s'affiche (page Baux, fiches
 * locataire / logement / immeuble) — règle « sections miroir ».
 */

import { useEffect, useState } from "react";
import { ArrowRightLeft, Check, FileSignature, Loader2, X } from "lucide-react";

import { Link } from "@/i18n/navigation";
import { authedFetch } from "@/lib/auth";
import { uploadBailDocument } from "@/components/immobilier/tal-avis";

type ImmeubleLite = {
  id: number;
  name: string;
  gestion_externe?: boolean | null;
};
type LogementLite = {
  id: number;
  numero?: string | null;
  status: string;
  loyer_demande?: number | null;
  location_en_chambres?: boolean | null;
};

type TransfertResult = {
  ancien_bail_id: number;
  ancien_bail_fin: string;
  nouveau_bail_id: number;
  nouveau_logement_id: number;
  nouveau_logement_numero: string | null;
  immeuble_id: number;
  dossier_id: number | null;
  depot_transfere: number;
};

const INPUT_CLS =
  "rounded-md border border-brand-800 bg-brand-950 px-2 py-1.5 text-xs text-white outline-none focus:border-accent-500";

const STATUT_LOGEMENT: Record<string, string> = {
  vacant: "vacant",
  occupe: "occupé",
  reserve: "réservé",
  hors_location: "hors location"
};

function money(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString("fr-CA", {
    style: "currency",
    currency: "CAD",
    maximumFractionDigits: 0
  });
}

/** 1er du mois suivant — la date de transfert la plus courante. */
function premierDuMoisSuivant(): string {
  const d = new Date();
  const y = d.getMonth() === 11 ? d.getFullYear() + 1 : d.getFullYear();
  const m = d.getMonth() === 11 ? 1 : d.getMonth() + 2;
  return `${y}-${String(m).padStart(2, "0")}-01`;
}

/** Prochain 30 juin « utile » (au moins ~3 mois de bail) — même règle
 *  que le formulaire « Assigner un bail » et que le serveur. */
function finParDefaut(debut: string, finActuelle?: string | null): string {
  if (finActuelle && debut && finActuelle > debut) return finActuelle;
  const d = debut ? new Date(debut + "T00:00:00") : new Date();
  let annee = d.getFullYear();
  if (d.getMonth() + 1 >= 4) annee += 1;
  return `${annee}-06-30`;
}

export function TransfertUniteButton({
  bailId,
  locataireNom,
  immeubleId,
  immeubleName,
  logementNumero,
  loyerActuel,
  finActuelle,
  onDone,
  className,
  compact = false
}: {
  bailId: number;
  locataireNom?: string | null;
  immeubleId: number;
  immeubleName?: string | null;
  logementNumero?: string | null;
  loyerActuel?: number | null;
  finActuelle?: string | null;
  /** Appelé après un transfert réussi (message à afficher). */
  onDone: (msg: string) => void;
  className?: string;
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title="Le locataire change de logement : son bail actuel se termine la veille, un nouveau bail (à signer) est créé sur la nouvelle unité et son dépôt le suit"
        className={
          className ||
          (compact
            ? "inline-flex items-center gap-1 rounded-lg border border-sky-500/40 bg-sky-500/10 px-2 py-0.5 text-[11px] font-semibold text-sky-200 transition hover:bg-sky-500/20"
            : "inline-flex items-center gap-1.5 rounded-lg border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 text-xs font-semibold text-sky-200 transition hover:bg-sky-500/20")
        }
      >
        <ArrowRightLeft className={compact ? "h-3 w-3" : "h-3.5 w-3.5"} />
        Transférer d&apos;unité
      </button>
      {open ? (
        <TransfertUniteModal
          bailId={bailId}
          locataireNom={locataireNom}
          immeubleId={immeubleId}
          immeubleName={immeubleName}
          logementNumero={logementNumero}
          loyerActuel={loyerActuel}
          finActuelle={finActuelle}
          onClose={() => setOpen(false)}
          onDone={(msg) => {
            setOpen(false);
            onDone(msg);
          }}
        />
      ) : null}
    </>
  );
}

function TransfertUniteModal({
  bailId,
  locataireNom,
  immeubleId,
  immeubleName,
  logementNumero,
  loyerActuel,
  finActuelle,
  onClose,
  onDone
}: {
  bailId: number;
  locataireNom?: string | null;
  immeubleId: number;
  immeubleName?: string | null;
  logementNumero?: string | null;
  loyerActuel?: number | null;
  finActuelle?: string | null;
  onClose: () => void;
  onDone: (msg: string) => void;
}) {
  const [immeubles, setImmeubles] = useState<ImmeubleLite[] | null>(null);
  const [immId, setImmId] = useState<number>(immeubleId);
  const [logements, setLogements] = useState<LogementLite[] | null>(null);
  const [logementId, setLogementId] = useState<number | null>(null);
  const [dateTransfert, setDateTransfert] = useState(premierDuMoisSuivant());
  const [dateFin, setDateFin] = useState(
    finParDefaut(premierDuMoisSuivant(), finActuelle)
  );
  const [loyer, setLoyer] = useState(
    loyerActuel != null ? String(loyerActuel) : ""
  );
  //: Dépôt actuellement détenu sur ce bail (page Dépôts) — suit le
  //: locataire par défaut.
  const [depotActuel, setDepotActuel] = useState<number | null>(null);
  const [depotSuit, setDepotSuit] = useState(true);
  const [depotNouveau, setDepotNouveau] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<TransfertResult | null>(null);
  //: Après le transfert : bail signé à joindre tout de suite.
  const [file, setFile] = useState<File | null>(null);
  const [joint, setJoint] = useState(false);
  const [busyDoc, setBusyDoc] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        const r = await authedFetch("/api/v1/immobilier/immeubles");
        if (!r.ok) throw new Error();
        const rows = (await r.json()) as ImmeubleLite[];
        // Pas de bail Kratos en gestion externe : on ne propose pas
        // ces immeubles (le serveur refuse de toute façon).
        setImmeubles(rows.filter((i) => !i.gestion_externe));
      } catch {
        setImmeubles([]);
      }
    })();
    void (async () => {
      try {
        const r = await authedFetch(
          `/api/v1/immobilier/depots/overview?immeuble_id=${immeubleId}`
        );
        if (!r.ok) return;
        const data = (await r.json()) as {
          rows: { bail_id: number; montant: number; statut: string }[];
        };
        const ligne = (data.rows || []).find(
          (x) => x.bail_id === bailId && x.statut === "detenu"
        );
        setDepotActuel(ligne ? ligne.montant : 0);
      } catch {
        setDepotActuel(0);
      }
    })();
  }, [bailId, immeubleId]);

  useEffect(() => {
    setLogements(null);
    setLogementId(null);
    void (async () => {
      try {
        const r = await authedFetch(
          `/api/v1/immobilier/immeubles/${immId}/logements`
        );
        if (!r.ok) throw new Error();
        const rows = (await r.json()) as LogementLite[];
        // Vacants d'abord — c'est presque toujours eux qu'on vise.
        rows.sort((a, b) => {
          const va = a.status === "vacant" ? 0 : 1;
          const vb = b.status === "vacant" ? 0 : 1;
          return (
            va - vb ||
            String(a.numero ?? "").localeCompare(String(b.numero ?? ""), "fr", {
              numeric: true
            })
          );
        });
        setLogements(rows);
      } catch {
        setLogements([]);
      }
    })();
  }, [immId]);

  const logementChoisi = (logements || []).find((l) => l.id === logementId);

  async function submit() {
    if (logementId == null) return;
    setSaving(true);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/baux/${bailId}/transferer`,
        {
          method: "POST",
          body: JSON.stringify({
            nouveau_logement_id: logementId,
            date_transfert: dateTransfert,
            date_fin: dateFin || null,
            loyer_mensuel: Number(loyer),
            transferer_depot: depotSuit && (depotActuel ?? 0) > 0,
            depot_garantie:
              depotSuit && (depotActuel ?? 0) > 0
                ? null
                : depotNouveau.trim()
                  ? Number(depotNouveau)
                  : null,
            notes: notes.trim() || null
          })
        }
      );
      if (!r.ok) {
        const t = await r.text();
        throw new Error(t.slice(0, 260) || `HTTP ${r.status}`);
      }
      setDone((await r.json()) as TransfertResult);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function joindreBail() {
    if (!done || !file) return;
    setBusyDoc(true);
    setErr(null);
    try {
      await uploadBailDocument({
        bailId: done.nouveau_bail_id,
        file,
        dateEntree: dateTransfert
      });
      setJoint(true);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusyDoc(false);
    }
  }

  const messageFin = done
    ? `Transfert fait : ${locataireNom || "le locataire"} passe au logement ${
        done.nouveau_logement_numero || done.nouveau_logement_id
      } le ${dateTransfert} (ancien bail terminé le ${done.ancien_bail_fin}${
        done.depot_transfere > 0
          ? `, dépôt de ${money(done.depot_transfere)} transféré`
          : ""
      }).${joint ? " Bail signé au dossier — nouveau bail actif." : " Le nouveau bail attend son PDF signé (kanban « Bail en signature »)."}`
    : "";

  return (
    <div
      className="fixed inset-0 z-[70] flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="my-8 w-full max-w-md rounded-2xl border border-brand-800 bg-brand-950 shadow-2xl">
        <div className="flex items-center justify-between border-b border-brand-800 px-5 py-3">
          <h2 className="text-sm font-bold uppercase tracking-wider text-sky-200">
            Transférer d&apos;unité
          </h2>
          <button
            type="button"
            onClick={() => (done ? onDone(messageFin) : onClose())}
            className="btn-ghost btn-xs"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {done ? (
          <div className="grid gap-3 p-5 text-sm text-white/80">
            <p className="flex items-start gap-2 font-semibold text-emerald-300">
              <Check className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                Transfert fait — nouveau bail créé sur le logement{" "}
                {done.nouveau_logement_numero || done.nouveau_logement_id}
                {done.depot_transfere > 0
                  ? `, dépôt de ${money(done.depot_transfere)} transféré`
                  : ""}
                .
              </span>
            </p>
            {joint ? (
              <p className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">
                Bail signé au dossier : le nouveau bail est actif et
                l&apos;unité est reloué.
              </p>
            ) : (
              <div className="rounded-lg border border-fuchsia-400/30 bg-fuchsia-500/10 p-3 text-xs text-fuchsia-100">
                <p className="mb-2">
                  Le nouveau bail est « proposé » (carte « Bail en
                  signature » dans Locations). Joins le bail signé dès
                  que tu l&apos;as — c&apos;est ce qui le rend actif.
                </p>
                <label className="block text-[11px] font-semibold text-white/60">
                  Bail signé (PDF)
                  <input
                    type="file"
                    accept="application/pdf"
                    onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                    className="mt-0.5 block w-full text-xs text-white/70 file:mr-2 file:rounded-md file:border-0 file:bg-brand-800 file:px-2.5 file:py-1.5 file:text-xs file:font-semibold file:text-white"
                  />
                </label>
                <button
                  type="button"
                  disabled={busyDoc || !file}
                  onClick={() => void joindreBail()}
                  className="btn-accent btn-sm mt-2 disabled:opacity-60"
                >
                  {busyDoc ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <FileSignature className="h-4 w-4" />
                  )}
                  Joindre le bail signé
                </button>
              </div>
            )}
            {err ? (
              <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                {err}
              </p>
            ) : null}
            <div className="flex flex-wrap items-center justify-end gap-2 border-t border-brand-800 pt-3 text-xs">
              {done.dossier_id != null ? (
                <Link
                  // eslint-disable-next-line @typescript-eslint/no-explicit-any
                  href={`/immobilier/locations?focus=${done.dossier_id}` as any}
                  className="mr-auto text-accent-500 underline-offset-2 hover:underline"
                >
                  Voir dans Locations →
                </Link>
              ) : null}
              <button
                type="button"
                onClick={() => onDone(messageFin)}
                className="btn-secondary btn-sm"
              >
                {joint ? "Fermer" : "Plus tard"}
              </button>
            </div>
          </div>
        ) : (
          <div className="grid gap-3 p-5">
            <p className="text-xs text-white/60">
              {locataireNom || "Locataire"}
              {immeubleName ? ` — ${immeubleName}` : ""}
              {logementNumero ? ` · Log. ${logementNumero}` : ""}
            </p>
            <p className="rounded-lg border border-sky-400/30 bg-sky-500/10 px-3 py-2 text-xs text-sky-200">
              Le bail actuel se termine la veille du transfert, un
              nouveau bail (à signer) est créé sur la nouvelle unité et
              le dépôt suit le locataire. L&apos;ancienne unité part en
              relocation automatiquement.
            </p>
            <div className="grid grid-cols-2 gap-3">
              <label className="text-[11px] font-semibold text-white/60">
                Immeuble
                <select
                  value={String(immId)}
                  onChange={(e) => setImmId(Number(e.target.value))}
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                >
                  {(immeubles || [{ id: immeubleId, name: immeubleName || "…" }]).map(
                    (i) => (
                      <option key={i.id} value={i.id}>
                        {i.name}
                      </option>
                    )
                  )}
                </select>
              </label>
              <label className="text-[11px] font-semibold text-white/60">
                Nouveau logement
                <select
                  value={logementId == null ? "" : String(logementId)}
                  onChange={(e) =>
                    setLogementId(e.target.value ? Number(e.target.value) : null)
                  }
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                >
                  <option value="">
                    {logements === null ? "Chargement…" : "Choisir…"}
                  </option>
                  {(logements || [])
                    .filter(
                      (l) =>
                        !(
                          immId === immeubleId &&
                          String(l.numero ?? "") === String(logementNumero ?? "")
                        )
                    )
                    .map((l) => (
                      <option key={l.id} value={l.id}>
                        {l.numero ?? `#${l.id}`} ·{" "}
                        {STATUT_LOGEMENT[l.status] || l.status}
                        {l.loyer_demande != null
                          ? ` · ${money(l.loyer_demande)}`
                          : ""}
                      </option>
                    ))}
                </select>
              </label>
            </div>
            {logementChoisi && logementChoisi.status !== "vacant" ? (
              <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-100/85">
                Ce logement n&apos;est pas vacant aujourd&apos;hui : le
                transfert passera seulement si personne n&apos;y a de bail
                sur la période.
              </p>
            ) : null}
            <div className="grid grid-cols-2 gap-3">
              <label className="text-[11px] font-semibold text-white/60">
                Date du transfert
                <input
                  type="date"
                  value={dateTransfert}
                  onChange={(e) => {
                    setDateTransfert(e.target.value);
                    if (e.target.value)
                      setDateFin(finParDefaut(e.target.value, finActuelle));
                  }}
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
              <label className="text-[11px] font-semibold text-white/60">
                Fin du nouveau bail
                <input
                  type="date"
                  value={dateFin}
                  onChange={(e) => setDateFin(e.target.value)}
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
              {(depotActuel ?? 0) > 0 ? (
                <label className="flex items-start gap-2 pt-4 text-[11px] font-semibold text-white/60">
                  <input
                    type="checkbox"
                    checked={depotSuit}
                    onChange={(e) => setDepotSuit(e.target.checked)}
                    className="mt-0.5"
                  />
                  <span>
                    Le dépôt de {money(depotActuel)} suit le locataire
                    <span className="block font-normal text-white/40">
                      Décoché : l&apos;ancien dépôt reste à rendre.
                    </span>
                  </span>
                </label>
              ) : (
                <label className="text-[11px] font-semibold text-white/60">
                  Dépôt de garantie ($)
                  <input
                    inputMode="decimal"
                    value={depotNouveau}
                    onChange={(e) => setDepotNouveau(e.target.value)}
                    placeholder="Optionnel"
                    className={`${INPUT_CLS} mt-0.5 block w-full`}
                  />
                </label>
              )}
            </div>
            {!depotSuit && (depotActuel ?? 0) > 0 ? (
              <label className="text-[11px] font-semibold text-white/60">
                Dépôt du nouveau bail ($)
                <input
                  inputMode="decimal"
                  value={depotNouveau}
                  onChange={(e) => setDepotNouveau(e.target.value)}
                  placeholder="Optionnel"
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
            ) : null}
            <label className="text-[11px] font-semibold text-white/60">
              Note (optionnel)
              <input
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                placeholder="Raison du transfert, entente particulière…"
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
                disabled={
                  saving ||
                  logementId == null ||
                  !dateTransfert ||
                  !dateFin ||
                  loyer.trim() === "" ||
                  Number.isNaN(Number(loyer))
                }
                onClick={() => void submit()}
                className="btn-accent btn-sm disabled:opacity-60"
              >
                {saving ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <ArrowRightLeft className="h-4 w-4" />
                )}
                Transférer
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
