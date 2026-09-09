"use client";

/**
 * Actions et constantes PARTAGÉES des paiements de loyer — utilisées par
 * la page Baux & paiements ET les fiches (immeuble, locataire) pour que
 * le comportement soit identique partout (directive « miroir
 * bidirectionnel » : une fiche = la page principale, ni plus ni moins).
 */

export type FraisMois = { id: number; montant: number; libelle: string };

/** Ligne de paiement minimale — commun aux Row des trois surfaces. */
export type PaiementRowLike = {
  loyer_mensuel: number;
  montant_paye?: number | null;
  frais_mois?: FraisMois[];
  etat: string; // "retard" | "attente" | "paye" | "partiel"
};

// DÛ du mois = loyer + frais ponctuels du mois (650 + 20 = 670 —
// retour Phil 2026-07-22 : « Marquer payé » doit couvrir les frais).
export function duMois(row: PaiementRowLike): number {
  return (
    Math.round(
      (row.loyer_mensuel +
        (row.frais_mois ?? []).reduce((s, f) => s + f.montant, 0)) *
        100
    ) / 100
  );
}

/** Montant du « Marquer payé » en 1 clic : le restant du mois — et pour
 *  une ligne « dette seulement » (bail terminé, mois après sa fin :
 *  loyer du mois à 0, retour client 2026-08-14), le SOLDE du bail. */
export function montantMarquerPaye(
  row: PaiementRowLike & { montant_paye?: number | null; solde_total?: number }
): number {
  const restant =
    Math.round((duMois(row) - (row.montant_paye ?? 0)) * 100) / 100;
  if (restant > 0) return restant;
  if ((row.solde_total ?? 0) > 0) return row.solde_total as number;
  return duMois(row);
}

/** Mois à imputer au paiement. Une ligne « dette seulement » (bail
 *  terminé avant le mois affiché) : le backend refuse un paiement hors
 *  période du bail → on impute au DERNIER mois couvert ; le trop-payé
 *  se répartit tout seul sur les mois impayés du bail. */
export function moisCouvertPourPaiement(
  row: { bail_termine_le?: string | null },
  moisAffiche: string
): string {
  const finMois = row.bail_termine_le?.slice(0, 7);
  if (finMois && finMois < moisAffiche) return finMois;
  return moisAffiche;
}

// Pastille (NON cliquable) du dernier avis de renouvellement du bail —
// mêmes libellés sur la page Baux et dans les fiches.
//
// Sémantique de couleur alignée sur le reste de Kratos (retour Phil
// 2026-08-13) : VERT = rien à faire. Un avis envoyé (ou une reconduction)
// est un dossier RÉGLÉ — on attend, on n'agit pas. Restent en alerte les
// seuls états qui demandent une action de notre part : refus (1 mois pour
// la fixation au TAL) et départ annoncé (relocation à ouvrir).
export const RENOUVELLEMENT_BADGES: Record<
  string,
  { label: string; cls: string }
> = {
  propose: { label: "Avis envoyé", cls: "badge-emerald" },
  accepte: { label: "Avis accepté", cls: "badge-emerald" },
  repute_accepte: { label: "Réputé accepté", cls: "badge-emerald" },
  refuse: { label: "Avis refusé", cls: "badge-rose" },
  depart: { label: "Départ annoncé", cls: "badge-amber" },
  reconduit: { label: "Reconduit", cls: "badge-emerald" },
  en_negociation: { label: "En négociation", cls: "badge-amber" }
};

/** Pastille bleue qui remplace le nom du locataire en gestion externe :
 *  la perception est déléguée au gestionnaire, on n'a pas de locataire
 *  nominatif de notre côté (retour Phil 2026-08-13). */
export function BadgeGestionExterne({
  nom,
  onRename
}: {
  /** Nom du locataire saisi sur le logement (retour Phil 2026-09-09 :
   *  « Gestion externe — Nom » pour cocher payé avec le nom à côté). */
  nom?: string | null;
  /** Présent = crayon pour saisir / changer le nom. */
  onRename?: () => void;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="badge badge-sky"
        title="Loyers perçus par la compagnie de gestion — le nom sert de repère pour le rapport mensuel"
      >
        Gestion externe{nom ? ` — ${nom}` : ""}
      </span>
      {onRename ? (
        <button
          type="button"
          onClick={onRename}
          className="text-[10px] text-white/40 hover:text-accent-500"
          title={nom ? "Changer le nom du locataire" : "Indiquer le nom du locataire"}
        >
          {nom ? "✎" : "+ nom"}
        </button>
      ) : null}
    </span>
  );
}

/**
 * Cellule « Loyer » unique à toutes les surfaces de paiement (page
 * Paiements, sous-page d'immeuble interne ET externe, fiche locataire).
 *
 * Trois niveaux empilés, demandés tels quels par Phil (2026-08-13) :
 * le loyer en gros, puis « Reçu » + l'encaissé, puis « Solde » + le
 * manquant. Solde à zéro ⇒ sobre (pas de rouge sur une ligne saine).
 */
export function CelluleLoyer({
  loyer,
  recu,
  solde,
  fmt,
  echeance,
  frais,
  onSupprimerFrais
}: {
  loyer: number;
  recu?: number | null;
  solde?: number | null;
  /** Formateur monétaire de la surface appelante (styles différents). */
  fmt: (n: number) => string;
  /** Bail TAL « Ou le ___ » — muet quand c'est le 1er. */
  echeance?: string | null;
  frais?: FraisMois[];
  onSupprimerFrais?: (fraisId: number) => void;
}) {
  const du = solde ?? 0;
  return (
    <div className="text-right">
      <div className="font-semibold tabular-nums text-white">{fmt(loyer)}</div>
      {echeance ? (
        <div className="text-[10px] font-normal text-white/60">{echeance}</div>
      ) : null}
      <div className="text-[10px] font-normal text-white/60">
        Reçu{" "}
        <span
          className={`tabular-nums font-semibold ${
            (recu ?? 0) > 0 ? "text-emerald-300" : "text-white/70"
          }`}
        >
          {fmt(recu ?? 0)}
        </span>
      </div>
      <div className="text-[10px] font-normal text-white/60">
        Solde{" "}
        <span
          className={`tabular-nums font-semibold ${
            du > 0 ? "text-rose-300" : "text-white/70"
          }`}
        >
          {fmt(du)}
        </span>
      </div>
      {(frais ?? []).map((f) => (
        <div
          key={f.id}
          // Ambre = frais qui s'ajoute au dû ; émeraude = CRÉDIT
          // (montant négatif) qui réduit le loyer (retour Phil
          // 2026-08-31).
          className={`flex items-center justify-end gap-1 text-[10px] ${
            f.montant < 0 ? "text-emerald-300" : "text-amber-300"
          }`}
        >
          <span title={f.libelle}>
            {f.montant < 0 ? "− " : "+ "}
            {fmt(Math.abs(f.montant))} {f.libelle}
          </span>
          {onSupprimerFrais ? (
            <button
              type="button"
              onClick={() => onSupprimerFrais(f.id)}
              title={f.montant < 0 ? "Retirer ce crédit" : "Retirer ce frais"}
              className="text-white/50 transition hover:text-rose-300"
            >
              ×
            </button>
          ) : null}
        </div>
      ))}
    </div>
  );
}

/** Options de correction d'un paiement — mêmes choix que la saisie
 *  (retour Phil 2026-07-31 : « Corriger » ne doit plus juste annuler
 *  et faire perdre la ligne). */
export function CorrectionOptions({
  r,
  busy,
  onMontant,
  onComplet,
  onRetirer,
  onClose
}: {
  r: { etat: string };
  busy: boolean;
  onMontant: () => void;
  onComplet: () => void;
  onRetirer: () => void;
  onClose: () => void;
}) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <button
        type="button"
        disabled={busy}
        onClick={onMontant}
        title="Ressaisir le montant réellement reçu (remplace les paiements du mois)"
        className="rounded-md border border-sky-500/40 bg-sky-500/10 px-2 py-0.5 text-[11px] font-semibold text-sky-300 hover:bg-sky-500/20 disabled:opacity-50"
      >
        Corriger le montant
      </button>
      {r.etat !== "paye" ? (
        <button
          type="button"
          disabled={busy}
          onClick={onComplet}
          title="Remplacer par un paiement complet du mois"
          className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50"
        >
          Payé au complet
        </button>
      ) : null}
      <button
        type="button"
        disabled={busy}
        onClick={onRetirer}
        title="Retirer les paiements du mois — la ligne redevient impayée"
        className="rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-0.5 text-[11px] font-semibold text-rose-300 hover:bg-rose-500/20 disabled:opacity-50"
      >
        Retirer
      </button>
      <button
        type="button"
        onClick={onClose}
        title="Fermer"
        className="px-1 text-[11px] text-white/40 hover:text-white/70"
      >
        ×
      </button>
    </span>
  );
}
