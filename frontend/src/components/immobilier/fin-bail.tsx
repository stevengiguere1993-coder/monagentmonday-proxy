"use client";

/**
 * Modales PARTAGÉES du cycle de vie d'un bail — extraites de la page
 * « Baux » (/immobilier/baux) pour que les fiches (locataire, logement,
 * immeuble) offrent EXACTEMENT le même comportement (directive
 * « miroir bidirectionnel ») :
 *   - FinBailModal : 2 modes — entente de résiliation signée en ligne
 *     (envoyer_avis) OU fin immédiate sans avis ;
 *   - CreerBailModal : créer un bail « proposé » (kanban Locations) ou
 *     « déjà en vigueur » (actif).
 */

import { useEffect, useState } from "react";
import { FileSignature, Info, Loader2, X } from "lucide-react";

import { Link } from "@/i18n/navigation";
import { ExpediteurResume } from "@/components/immobilier/apercu-envoi";
import { authedFetch } from "@/lib/auth";

/** Ligne renvoyée par l'API `GET /api/v1/immobilier/suivi-baux` — une
 *  ligne PAR LOGEMENT (bail actif + prochain). Utilisée par la page Baux
 *  (/immobilier/baux) et l'onglet Baux de la fiche immeuble. */
export type SuiviBailRow = {
  logement_id: number;
  logement_numero: string;
  logement_status: string;
  /** « Louer indéfiniment (chambre) » — loyer figé, bail au mois. */
  logement_en_chambres?: boolean;
  immeuble_id: number;
  immeuble_name: string;
  bail_id: number | null;
  locataire_id: number | null;
  locataire_nom: string | null;
  date_debut: string | null;
  date_fin: string | null;
  loyer_mensuel: number | null;
  au_mois: boolean | null;
  /** Jour du mois où le loyer est payable (bail TAL « Ou le ___ »). */
  jour_echeance: number | null;
  document_id: number | null;
  signed_at: string | null;
  prochain_bail_id: number | null;
  prochain_locataire_id: number | null;
  prochain_locataire_nom: string | null;
  prochain_date_debut: string | null;
  prochain_loyer: number | null;
  prochain_document_id: number | null;
  prochain_statut: string | null;
  dossier_id: number | null;
  dossier_statut: string | null;
  /** Motif d'exception « aucun bail à joindre », s'il a été déclaré. */
  sans_document_motif?: string | null;
  resiliation_en_cours: boolean;
  resiliation_date: string | null;
  /** Suivi de l'entente en attente de signature : une entente envoyée
   *  mais jamais ouverte n'a pas le même sens qu'une entente ouverte et
   *  laissée sans réponse — dans le second cas, le locataire a vu et
   *  n'a pas signé (retour Phil 2026-08-19). */
  resiliation_document_id?: number | null;
  resiliation_envoye_le?: string | null;
  resiliation_ouvert_le?: string | null;
  renouvellement_status: string | null;
  renouvellement_avis_document_id: number | null;
};

// Mêmes étapes que le kanban Locations — même donnée, changer ici
// change là-bas (et vice-versa).
export const KANBAN_STATUTS: Array<{ id: string; label: string }> = [
  { id: "avis_recu", label: "Départ confirmé" },
  { id: "annonce_publiee", label: "Annonce publiée" },
  { id: "visites", label: "Visite prévue" },
  { id: "candidat_retenu", label: "Candidat retenu" },
  { id: "bail_a_envoyer", label: "Bail à envoyer" },
  { id: "bail_envoye", label: "Bail envoyé — à signer" },
  { id: "reloue", label: "Reloué" }
];

// Mêmes points de couleur que les colonnes du kanban Locations.
const KANBAN_STATUT_DOT: Record<string, string> = {
  avis_recu: "bg-amber-400",
  annonce_publiee: "bg-sky-400",
  visites: "bg-violet-400",
  candidat_retenu: "bg-blue-400",
  bail_a_envoyer: "bg-orange-400",
  bail_envoye: "bg-fuchsia-400",
  reloue: "bg-emerald-400"
};

/** Pastille LECTURE SEULE du statut de relocation + lien vers la
 *  source. Directive « miroir bidirectionnel » : on VOIT le statut
 *  partout (page Baux, fiches), mais on le MODIFIE seulement à la
 *  source — le kanban Locations (retour Phil 2026-08-13).
 *
 *  `dossierId` (m3, audit 2026-08-13) : le lien cible la carte —
 *  `?focus={id}` fait défiler et surligner la carte dans le kanban. */
export function RelocationStatutPastille({
  statut,
  dossierId
}: {
  statut: string;
  dossierId?: number | null;
}) {
  const label =
    KANBAN_STATUTS.find((s) => s.id === statut)?.label ?? statut;
  const href =
    dossierId != null
      ? `/immobilier/locations?focus=${dossierId}`
      : "/immobilier/locations";
  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
      <span
        title="Étape du kanban Locations — se modifie dans la page Locations"
        className="inline-flex items-center gap-1.5 rounded-full border border-brand-700 bg-brand-950 px-2.5 py-1 text-[11px] font-semibold text-white/80"
      >
        <span
          className={`h-1.5 w-1.5 flex-shrink-0 rounded-full ${
            KANBAN_STATUT_DOT[statut] ?? "bg-white/40"
          }`}
        />
        {label}
      </span>
      <Link
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        href={href as any}
        className="text-[11px] font-medium text-accent-500 underline-offset-2 hover:underline"
        title="La relocation se pilote dans le kanban Locations"
      >
        Ouvrir dans Locations →
      </Link>
    </span>
  );
}

const INPUT_CLS =
  "rounded-md border border-brand-800 bg-brand-950 px-2 py-1.5 text-xs text-white outline-none focus:border-accent-500";

// ─── « Louer indéfiniment (chambre) » ───────────────────────────────────
//
// Anciennement « Loué en chambre » : le flag Logement.location_en_chambres
// (nom de colonne inchangé) veut dire « on loue ça au mois, pour toujours ».
// Conséquences, toutes portées par le BAIL (au_mois) :
//   - loyer figé, aucun avis d'augmentation à produire ;
//   - jamais dans les échéances ni dans le hub Renouvellements ;
//   - tarif de gestion « chambre » (moindre) au contrat de gestion.
// Libellé et texte d'aide sont CENTRALISÉS ici — un seul endroit à
// changer (retour Phil 2026-08-13).

export const LOUER_INDEFINIMENT_LABEL = "Louer indéfiniment (chambre)";

export const LOUER_INDEFINIMENT_INFO =
  "Le logement est loué au mois, indéfiniment. Le loyer reste le même " +
  "— aucun avis d'augmentation n'est attendu et l'unité n'apparaît pas " +
  "dans les renouvellements. Le tarif de gestion appliqué est celui des " +
  "chambres (moindre).";

/** Bulle d'information — même boîte « info » que le reste du pôle. */
export function LouerIndefinimentBulle({
  className,
  children
}: {
  className?: string;
  children?: React.ReactNode;
}) {
  return (
    <div
      className={`flex items-start gap-2 rounded-lg border border-sky-400/30 bg-sky-500/10 px-3 py-2 text-[11px] leading-relaxed text-sky-200 ${
        className ?? ""
      }`}
    >
      <Info className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
      <span>{children ?? LOUER_INDEFINIMENT_INFO}</span>
    </div>
  );
}

// ─── Jour d'échéance du loyer (bail TAL « Ou le ___ ») ──────────────────
//
// Le bail TAL a une case « le 1er jour du mois » ET un champ « Ou le
// ___ » : la quasi-totalité des baux sont payables le 1er, mais pas tous
// (ex. le garage du 8900 St-Hubert, payable le 12). Défaut = 1 → rien ne
// change pour les baux existants. Borné à 28 côté API (février).

export const JOUR_ECHEANCE_DEFAUT = 1;
export const JOUR_ECHEANCE_MAX = 28;

/** Options 1 → 28 du sélecteur (partagées par tous les formulaires). */
export const JOURS_ECHEANCE: number[] = Array.from(
  { length: JOUR_ECHEANCE_MAX },
  (_, i) => i + 1
);

/** « payable le 12 » — `null` quand c'est le 1er (on n'alourdit pas). */
export function echeanceLabel(jour?: number | null): string | null {
  const j = Number(jour ?? JOUR_ECHEANCE_DEFAUT);
  if (!Number.isFinite(j) || j === JOUR_ECHEANCE_DEFAUT) return null;
  return `payable le ${j}`;
}

/** Champ « Loyer payable le » — même rendu partout (création + édition).
 *  Les classes sont surchargeables pour épouser le style local du
 *  formulaire hôte (modales `INPUT_CLS` vs formulaires `.input`). */
export function JourEcheanceField({
  value,
  onChange,
  disabled,
  labelClassName,
  selectClassName,
  hint = true
}: {
  value: number;
  onChange: (jour: number) => void;
  disabled?: boolean;
  labelClassName?: string;
  selectClassName?: string;
  hint?: boolean;
}) {
  return (
    <label
      className={labelClassName ?? "text-[11px] font-semibold text-white/60"}
    >
      Loyer payable le
      <select
        value={String(value || JOUR_ECHEANCE_DEFAUT)}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
        className={selectClassName ?? `${INPUT_CLS} mt-0.5 block w-full`}
      >
        {JOURS_ECHEANCE.map((j) => (
          <option key={j} value={j}>
            {j === 1 ? "1er du mois" : `${j} du mois`}
          </option>
        ))}
      </select>
      {hint ? (
        <span className="mt-0.5 block text-[10px] font-normal text-white/40">
          Habituellement le 1er du mois.
        </span>
      ) : null}
    </label>
  );
}

/** Mention « payable le 12 » CLIQUABLE sous un loyer : au clic, un
 *  sélecteur qui PATCH le bail. Rien d'affiché quand c'est le 1er —
 *  l'édition passe alors par le crayon (au survol de la cellule).
 *
 *  Utilisé tel quel par la page Baux ET l'onglet Baux de la fiche
 *  immeuble (directive « miroir bidirectionnel »). */
export function JourEcheanceInline({
  bailId,
  jour,
  onChanged,
  compact = false
}: {
  bailId: number | null;
  jour?: number | null;
  onChanged?: () => void | Promise<void>;
  /** Pilule en ligne (fiches) plutôt que ligne pleine largeur (tableaux). */
  compact?: boolean;
}) {
  const courant = Number(jour ?? JOUR_ECHEANCE_DEFAUT) || JOUR_ECHEANCE_DEFAUT;
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);

  if (bailId == null) return null;

  async function enregistrer(nouveau: number) {
    if (nouveau === courant) {
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      const r = await authedFetch(`/api/v1/immobilier/baux/${bailId}`, {
        method: "PATCH",
        body: JSON.stringify({ jour_echeance: nouveau })
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setEditing(false);
      await onChanged?.();
    } catch {
      window.alert("Jour d'échéance non enregistré — réessaie.");
    } finally {
      setBusy(false);
    }
  }

  if (editing) {
    return (
      <select
        autoFocus
        disabled={busy}
        value={String(courant)}
        onBlur={() => setEditing(false)}
        onChange={(e) => void enregistrer(Number(e.target.value))}
        className={`${INPUT_CLS} font-sans ${
          compact ? "inline-block" : "mt-0.5 w-full"
        }`}
      >
        {JOURS_ECHEANCE.map((j) => (
          <option key={j} value={j}>
            {j === 1 ? "payable le 1er" : `payable le ${j}`}
          </option>
        ))}
      </select>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      title="Jour du mois où le loyer est payable (bail TAL « Ou le ___ ») — cliquer pour modifier"
      className={
        compact
          ? "cursor-pointer rounded-md border border-brand-700 px-2 py-0.5 text-[11px] font-medium text-white/50 transition hover:border-brand-600 hover:text-white/80"
          : `mt-0.5 block w-full cursor-pointer font-sans text-[10px] font-normal transition ${
              courant === JOUR_ECHEANCE_DEFAUT
                ? // Le 1er = le cas normal : rien à l'écran, révélé au
                  // survol pour rester modifiable sans alourdir.
                  "text-transparent hover:text-white/40"
                : "text-white/45 hover:text-white/70"
            }`
      }
    >
      {echeanceLabel(courant) ?? "payable le 1er"}
    </button>
  );
}

// ─── Mettre fin au bail (entente signée en ligne OU fin immédiate) ──────

export function FinBailModal({
  bailId,
  locataireNom,
  immeubleName,
  logementNumero,
  onClose,
  onDone
}: {
  bailId: number;
  locataireNom?: string | null;
  immeubleName?: string | null;
  logementNumero?: string | null;
  onClose: () => void;
  onDone: (msg: string) => void;
}) {
  const [dateFin, setDateFin] = useState("");
  const [mode, setMode] = useState<"avis" | "immediat">("avis");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function confirmer() {
    if (!dateFin) return;
    setBusy(true);
    setErr(null);
    try {
      const res = await authedFetch(
        `/api/v1/immobilier/baux/${bailId}/resilier`,
        {
          method: "POST",
          body: JSON.stringify({
            date_fin: dateFin,
            ouvrir_relocation: true,
            envoyer_avis: mode === "avis"
          })
        }
      );
      if (!res.ok) {
        const t = await res.text();
        throw new Error(t.slice(0, 240) || `HTTP ${res.status}`);
      }
      const d = (await res.json()) as {
        statut?: string;
        relocation_ouverte?: boolean;
      };
      if (d.statut === "resiliation_en_cours") {
        onDone(
          "Entente de résiliation envoyée pour signature — le bail reste actif jusqu'à la signature, puis se résilie et la relocation s'ouvre automatiquement."
        );
      } else {
        let msg = `Bail terminé au ${dateFin}.`;
        if (d.relocation_ouverte)
          msg += " Dossier de relocation ouvert automatiquement.";
        onDone(msg);
      }
    } catch (e) {
      setErr((e as Error).message);
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
          <h2 className="text-sm font-bold uppercase tracking-wider text-rose-300">
            Mettre fin au bail
          </h2>
          <button type="button" onClick={onClose} className="btn-ghost btn-xs">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="grid gap-3 p-5">
          <p className="text-xs text-white/60">
            {locataireNom || "Locataire"}
            {immeubleName ? ` — ${immeubleName}` : ""}
            {logementNumero ? ` · Log. ${logementNumero}` : ""}
          </p>
          <label className="text-[11px] font-semibold text-white/60">
            Date de fin du bail
            <input
              type="date"
              value={dateFin}
              onChange={(e) => setDateFin(e.target.value)}
              className={`${INPUT_CLS} mt-0.5 block w-full`}
            />
          </label>
          <div className="grid gap-1.5">
            <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-brand-800 px-3 py-2 text-xs text-white/75">
              <input
                type="radio"
                checked={mode === "avis"}
                onChange={() => setMode("avis")}
                className="mt-0.5"
              />
              <span>
                <span className="font-semibold text-white">
                  Entente de résiliation SIGNÉE en ligne
                </span>{" "}
                — le locataire reçoit l&apos;entente par courriel et la
                signe (suivi d&apos;ouverture et de signature). Le bail
                reste actif jusqu&apos;à la signature, puis se résilie à
                la date choisie.
              </span>
            </label>
            <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-brand-800 px-3 py-2 text-xs text-white/75">
              <input
                type="radio"
                checked={mode === "immediat"}
                onChange={() => setMode("immediat")}
                className="mt-0.5"
              />
              <span>
                <span className="font-semibold text-white">
                  Fin immédiate sans avis
                </span>{" "}
                — déguerpissement, entente verbale : le bail se termine à
                la date choisie, rien n&apos;est envoyé.
              </span>
            </label>
          </div>
          {mode === "avis" ? (
            /* L'entente part par courriel au locataire : on montre d'où
               elle part et où atterrit sa réponse, comme partout
               ailleurs (retour Phil 2026-08-19). */
            <ExpediteurResume note="L'envoi sera tracé dans Communications et sur la fiche du locataire." />
          ) : null}
          <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
            ⚠️ Un dossier de relocation s&apos;ouvrira AUTOMATIQUEMENT
            dans le kanban Locations (dès maintenant en fin immédiate, à
            la signature pour l&apos;entente).
          </p>
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
              disabled={busy || !dateFin}
              onClick={() => void confirmer()}
              className="btn-accent btn-sm disabled:opacity-60"
            >
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
              {mode === "avis"
                ? "Envoyer l'entente pour signature"
                : "Mettre fin au bail"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Créer un nouveau bail ──────────────────────────────────────────────

export type CreerBailStatut = "propose" | "actif";

export function CreerBailModal({
  logementId,
  immeubleName,
  logementNumero,
  logementEnChambres,
  onClose,
  onDone
}: {
  logementId: number;
  immeubleName?: string | null;
  logementNumero?: string | null;
  /** Logement « Louer indéfiniment (chambre) » → bail au mois imposé. */
  logementEnChambres?: boolean | null;
  onClose: () => void;
  onDone: (statut: CreerBailStatut) => void;
}) {
  const indefini = Boolean(logementEnChambres);
  const [modeExistant, setModeExistant] = useState(true);
  const [dispo, setDispo] = useState<
    { id: number; full_name: string }[] | null
  >(null);
  const [q, setQ] = useState("");
  const [locId, setLocId] = useState<number | null>(null);
  const [nom, setNom] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [debut, setDebut] = useState("");
  const [fin, setFin] = useState("");
  const [loyer, setLoyer] = useState("");
  const [depot, setDepot] = useState("");
  const [depotRecuLe, setDepotRecuLe] = useState("");
  const [depotDetenteur, setDepotDetenteur] = useState("");
  // Bail TAL « Ou le ___ » : 1er du mois pour l'immense majorité.
  const [jourEcheance, setJourEcheance] = useState(JOUR_ECHEANCE_DEFAUT);
  // Statut de création UNIFIÉ (même défaut que la page Baux) : proposé
  // → passe par le kanban Locations ; actif → bail déjà signé/en vigueur.
  const [statut, setStatut] = useState<CreerBailStatut>("propose");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!modeExistant || dispo !== null) return;
    void (async () => {
      try {
        const res = await authedFetch("/api/v1/immobilier/locataires");
        setDispo(
          res.ok
            ? ((await res.json()) as { id: number; full_name: string }[])
            : []
        );
      } catch {
        setDispo([]);
      }
    })();
  }, [modeExistant, dispo]);

  async function creer() {
    setBusy(true);
    setErr(null);
    try {
      let locataireId = locId;
      if (!modeExistant) {
        const rl = await authedFetch("/api/v1/immobilier/locataires", {
          method: "POST",
          body: JSON.stringify({
            full_name: nom.trim(),
            email: email.trim() || null,
            phone: phone.trim() || null
          })
        });
        if (!rl.ok) {
          const t = await rl.text();
          throw new Error(t.slice(0, 240) || `HTTP ${rl.status}`);
        }
        locataireId = ((await rl.json()) as { id: number }).id;
      }
      if (locataireId == null) throw new Error("Choisis un locataire.");
      const rb = await authedFetch("/api/v1/immobilier/baux", {
        method: "POST",
        body: JSON.stringify({
          logement_id: logementId,
          locataire_id: locataireId,
          date_debut: debut,
          date_fin: fin,
          loyer_mensuel: Number(loyer),
          depot_garantie: depot.trim() ? Number(depot) : null,
          depot_recu_le: depotRecuLe || null,
          depot_detenteur: depotDetenteur.trim() || null,
          jour_echeance: jourEcheance,
          status: statut,
          // Le flag du logement fait foi (le serveur le réimpose de
          // toute façon) — pas de bascule manuelle à la création.
          au_mois: indefini ? true : null
        })
      });
      if (!rb.ok) {
        const t = await rb.text();
        throw new Error(t.slice(0, 240) || `HTTP ${rb.status}`);
      }
      onDone(statut);
    } catch (e) {
      setErr((e as Error).message);
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
            Créer un nouveau bail
          </h2>
          <button type="button" onClick={onClose} className="btn-ghost btn-xs">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="grid gap-3 p-5">
          <p className="text-xs text-white/60">
            {immeubleName || "Immeuble"} · Log. {logementNumero || "—"} —
            en « proposé », la carte apparaît au kanban Locations
            (« Bail à envoyer ») : importe ensuite le PDF signé (CORPIQ)
            pour le rendre actif.
          </p>
          {indefini ? (
            <LouerIndefinimentBulle>
              <strong className="font-semibold text-white">
                Ce logement est loué indéfiniment (chambre)
              </strong>{" "}
              : le bail sera créé <strong>au mois</strong> — loyer figé,
              reconduction automatique, aucun avis de renouvellement.{" "}
              {LOUER_INDEFINIMENT_INFO}
            </LouerIndefinimentBulle>
          ) : null}
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
              Locataire existant
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
            <div className="grid gap-1.5">
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Rechercher le locataire…"
                className={`${INPUT_CLS} w-full`}
              />
              <div className="max-h-36 overflow-y-auto rounded-md border border-brand-800">
                {(dispo || [])
                  .filter((l) =>
                    l.full_name.toLowerCase().includes(q.toLowerCase())
                  )
                  .slice(0, 25)
                  .map((l) => (
                    <button
                      key={l.id}
                      type="button"
                      onClick={() => setLocId(l.id)}
                      className={`block w-full px-3 py-1.5 text-left text-xs ${
                        locId === l.id
                          ? "bg-emerald-500/20 font-semibold text-emerald-200"
                          : "text-white/75 hover:bg-brand-900"
                      }`}
                    >
                      {l.full_name}
                    </button>
                  ))}
                {dispo === null ? (
                  <p className="px-3 py-1.5 text-xs text-white/40">
                    Chargement…
                  </p>
                ) : null}
              </div>
            </div>
          ) : (
            <div className="grid gap-2">
              <label className="text-[11px] font-semibold text-white/60">
                Nom complet *
                <input
                  value={nom}
                  onChange={(e) => setNom(e.target.value)}
                  className={`${INPUT_CLS} mt-0.5 block w-full`}
                />
              </label>
              <div className="grid grid-cols-2 gap-2">
                <label className="text-[11px] font-semibold text-white/60">
                  Courriel
                  <input
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className={`${INPUT_CLS} mt-0.5 block w-full`}
                  />
                </label>
                <label className="text-[11px] font-semibold text-white/60">
                  Téléphone
                  <input
                    value={phone}
                    onChange={(e) => setPhone(e.target.value)}
                    className={`${INPUT_CLS} mt-0.5 block w-full`}
                  />
                </label>
              </div>
            </div>
          )}
          <div className="grid grid-cols-2 gap-2">
            <label className="text-[11px] font-semibold text-white/60">
              Début du bail *
              <input
                type="date"
                value={debut}
                onChange={(e) => setDebut(e.target.value)}
                className={`${INPUT_CLS} mt-0.5 block w-full`}
              />
            </label>
            <label className="text-[11px] font-semibold text-white/60">
              Fin du bail *
              <input
                type="date"
                value={fin}
                onChange={(e) => setFin(e.target.value)}
                className={`${INPUT_CLS} mt-0.5 block w-full`}
              />
              {indefini ? (
                <span className="mt-0.5 block text-[10px] font-normal text-sky-300/80">
                  Formalité : le bail se reconduit tout seul au même
                  loyer, cette date ne déclenche rien.
                </span>
              ) : null}
            </label>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-[11px] font-semibold text-white/60">
              Loyer mensuel ($) *
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
            <label className="text-[11px] font-semibold text-white/60">
              Dépôt reçu le
              <input
                type="date"
                value={depotRecuLe}
                onChange={(e) => setDepotRecuLe(e.target.value)}
                className={`${INPUT_CLS} mt-0.5 block w-full`}
              />
            </label>
            <label className="text-[11px] font-semibold text-white/60">
              Dépôt détenu par
              <input
                value={depotDetenteur}
                onChange={(e) => setDepotDetenteur(e.target.value)}
                placeholder="ex. compte en fidéicommis"
                className={`${INPUT_CLS} mt-0.5 block w-full`}
              />
            </label>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <JourEcheanceField
              value={jourEcheance}
              onChange={setJourEcheance}
            />
          </div>
          <div className="grid gap-1.5">
            <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-brand-800 px-3 py-2 text-xs text-white/75">
              <input
                type="radio"
                checked={statut === "propose"}
                onChange={() => setStatut("propose")}
                className="mt-0.5"
              />
              <span>
                <span className="font-semibold text-white">
                  À faire suivre via Locations (proposé)
                </span>{" "}
                — la carte apparaît au kanban (« Bail à envoyer ») ;
                importe le PDF signé pour rendre le bail actif.
              </span>
            </label>
            <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-brand-800 px-3 py-2 text-xs text-white/75">
              <input
                type="radio"
                checked={statut === "actif"}
                onChange={() => setStatut("actif")}
                className="mt-0.5"
              />
              <span>
                <span className="font-semibold text-white">
                  Ce bail est déjà en vigueur (signé)
                </span>{" "}
                — créé directement ACTIF, sans passer par le kanban
                Locations (le logement passe « occupé »).
              </span>
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
                busy ||
                (modeExistant ? locId == null : !nom.trim()) ||
                !debut ||
                !fin ||
                loyer.trim() === "" ||
                Number.isNaN(Number(loyer))
              }
              onClick={() => void creer()}
              className="btn-accent btn-sm disabled:opacity-60"
            >
              {busy ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <FileSignature className="h-4 w-4" />
              )}
              Créer le bail
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * Annuler un DÉPART déjà confirmé — l'opération inverse de FinBailModal.
 *
 * Retour Phil 2026-08-19 : sur un bail dont le départ est acté, le
 * bouton « Mettre fin au bail » restait proposé — un geste sans effet
 * qui laisse croire à une action. Ce qui manque à ce moment-là, c'est
 * le retour en arrière : le locataire a changé d'idée.
 *
 * Ce n'est pas anodin, d'où la liste explicite des conséquences avant
 * de confirmer. Le serveur refuse en plus l'annulation dès qu'un
 * candidat est retenu : on aurait deux locataires sur la même unité.
 */
export function AnnulerDepartModal({
  row,
  onClose,
  onDone
}: {
  row: SuiviBailRow;
  onClose: () => void;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function annuler() {
    if (row.bail_id == null) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/baux/${row.bail_id}/annuler-depart`,
        { method: "POST" }
      );
      if (!r.ok) {
        const t = await r.text();
        let msg = t.slice(0, 400) || `HTTP ${r.status}`;
        try {
          const j = JSON.parse(t) as { detail?: string };
          if (j.detail) msg = j.detail;
        } catch {
          /* réponse non JSON : on garde le texte brut */
        }
        throw new Error(msg);
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
          <h2 className="text-sm font-bold uppercase tracking-wider text-amber-300">
            Annuler le départ
          </h2>
          <button type="button" onClick={onClose} className="btn-ghost btn-xs">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="space-y-3 px-5 py-4 text-sm">
          <p className="text-white/80">
            Le départ de{" "}
            <b className="text-white">
              {row.locataire_nom || "ce locataire"}
            </b>{" "}
            ({row.immeuble_name} · {row.logement_numero}) a été confirmé.
            Annuler signifie qu&apos;il <b className="text-white">reste</b>.
          </p>
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-xs text-amber-100/85">
            <p className="mb-1.5 font-semibold">Ce qui va se passer :</p>
            <ul className="list-inside list-disc space-y-1">
              <li>le dossier de relocation passe à « annulé » ;</li>
              <li>
                si le bail avait été fermé, il redevient <b>actif</b> ;
              </li>
              <li>le logement redevient occupé.</li>
            </ul>
          </div>
          <p className="text-[11px] text-white/45">
            Si un candidat est déjà retenu pour ce logement,
            l&apos;annulation sera refusée — il faut d&apos;abord régler
            son sort dans la page Locations, sinon deux locataires se
            retrouveraient sur la même unité.
          </p>
          {err ? (
            <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
              {err}
            </p>
          ) : null}
        </div>
        <div className="flex justify-end gap-2 border-t border-brand-800 px-5 py-3">
          <button type="button" onClick={onClose} className="btn-secondary btn-sm">
            Garder le départ
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void annuler()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-xs font-semibold text-amber-300 transition hover:bg-amber-500/20 disabled:opacity-50"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            Oui, le locataire reste
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * État d'une entente de résiliation en attente de signature.
 *
 * Retour Phil 2026-08-19 : « si c'est pas signé il est pas encore mis
 * fin » — mais la ligne rouge ne disait pas si le locataire avait seulement
 * ouvert le lien. Or c'est toute la différence : une entente jamais
 * ouverte se relance, une entente ouverte et non signée se discute.
 */
export function ResiliationSuivi({
  row
}: {
  row: Pick<
    SuiviBailRow,
    | "resiliation_date"
    | "resiliation_envoye_le"
    | "resiliation_ouvert_le"
  >;
}) {
  const jour = (iso?: string | null) => {
    if (!iso) return "";
    const d = new Date(iso);
    return Number.isNaN(d.getTime())
      ? iso.slice(0, 10)
      : d.toLocaleDateString("fr-CA", { day: "numeric", month: "short" });
  };
  const etat = row.resiliation_ouvert_le
    ? `ouverte le ${jour(row.resiliation_ouvert_le)}, pas signée`
    : row.resiliation_envoye_le
      ? `envoyée le ${jour(row.resiliation_envoye_le)}, pas encore ouverte`
      : "signature attendue";
  return (
    <span
      className="badge badge-rose"
      title="Le bail n'est PAS terminé tant que l'entente n'est pas signée"
    >
      Résiliation en cours — {etat}
      {row.resiliation_date ? ` (fin le ${row.resiliation_date})` : ""}
    </span>
  );
}
