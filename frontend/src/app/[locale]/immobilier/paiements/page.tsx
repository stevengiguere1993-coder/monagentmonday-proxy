"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ClipboardList,
  Clock,
  KeyRound,
  Loader2,
  Mail,
  Phone,
  Scale,
  Search
} from "lucide-react";

import { Link } from "@/i18n/navigation";
import { ApercuEnvoiModal } from "@/components/immobilier/apercu-envoi";
import { authedFetch } from "@/lib/auth";
import { ImmobilierTopbar, useImmobilierLayout } from "../layout";
import { BandeauAvisRenouvellement } from "@/components/immobilier/bandeau-avis";
import { BandeauBailManquant } from "@/components/immobilier/bandeau-bail-manquant";
import { BandeauDepotARembourser } from "@/components/immobilier/bandeau-depot";
import { echeanceLabel } from "@/components/immobilier/fin-bail";
import { BoutonExport } from "@/components/immobilier/bouton-export";
import {
  BadgeGestionExterne,
  CelluleLoyer,
  CorrectionOptions,
  duMois,
  moisCouvertPourPaiement,
  montantMarquerPaye
} from "@/components/immobilier/paiements-actions";
import {
  CrochetFilBancaire,
  EncartValidationBancaire,
  PastilleValidationBancaire,
  ValidationEtat,
  chargerValidationBancaire,
  indexValidations
} from "@/components/immobilier/validation-bancaire";

/**
 * Paiements — vue transversale « collection des loyers »
 * (/immobilier/paiements).
 *
 * Tous les baux ACTIFS du portefeuille croisés avec les paiements du
 * mois choisi : qui a payé, qui est en retard, marquer payé en 1 clic.
 * Les retards remontent en premier.
 *
 * Le SUIVI des baux eux-mêmes (créer, importer le PDF, mettre fin) vit
 * sur la page « Baux » (/immobilier/baux).
 *
 * Les immeubles en GESTION EXTERNE y figurent aussi (retour Phil
 * 2026-08-13) : même tableau, mais une pastille bleue « Gestion externe »
 * à la place du locataire — la perception est déléguée, donc pas de
 * relance ni de frais sur ces lignes.
 */

type Row = {
  bail_id: number;
  immeuble_id: number;
  immeuble_name: string;
  logement_id?: number | null;
  logement_numero: string | null;
  locataire_id: number | null;
  locataire_name: string | null;
  locataire_phone: string | null;
  locataire_email: string | null;
  /** Départ ACTÉ : ce locataire part à cette date. */
  libre_le: string | null;
  loyer_mensuel: number;
  /** Jour du mois où le loyer est payable (bail TAL « Ou le ___ »). */
  jour_echeance?: number | null;
  paiement_id: number | null;
  montant_paye: number | null;
  paye_le: string | null;
  etat: string; // "retard" | "attente" | "paye" | "partiel" | "vacant"
  /** Ligne de logement VACANT (bail_id vaut 0) : statut exact du
   *  logement ("vacant" ou "reserve") pour l'étiquette. */
  logement_statut?: string | null;
  /** Dossier ouvert au TAL sur ce bail (non-paiement) — badge + coche
   *  pour que l'équipe voie que le recours est lancé. */
  tal_dossier_ouvert_le?: string | null;
  //: Mois affiché payé, mais un mois antérieur du bail impayé.
  solde_anterieur?: boolean;
  /** Bail résilié/terminé en cours de mois : la ligne reste dans le
   *  mois couvert avec un badge « Bail terminé le X » (M7). */
  bail_statut?: string;
  bail_termine_le?: string | null;
  //: LE bail courant (imm_documents) — clic = l'ouvrir.
  document_id?: number | null;
  // Frais ponctuels du mois (retard…) + solde cumulatif dû sur le bail.
  frais_mois?: { id: number; montant: number; libelle: string }[];
  solde_total?: number;
  nb_relances: number;
  derniere_relance_le: string | null;
  // Prochain locataire (transition) — bail futur / en préparation.
  prochain_nom?: string | null;
  prochain_loyer?: number | null;
  prochain_debut?: string | null;
  prochain_statut?: string | null;
  /** Ligne d'un immeuble en gestion externe : suivi PAR LOGEMENT, sans
   *  locataire nominatif ni relance (bail_id vaut alors 0). */
  gestion_externe?: boolean;
};

/** Ligne brute de /loyers/externes — repliée dans `Row` pour l'affichage. */
type RowExterne = {
  immeuble_id: number;
  immeuble_name: string;
  logement_id: number;
  logement_numero: string;
  loyer_mensuel: number;
  montant_paye: number | null;
  paye_le: string | null;
  etat: string;
  solde_total: number;
  solde_anterieur?: boolean;
  locataire_nom?: string | null;
};

type OverviewExterne = {
  mois: string;
  rows: RowExterne[];
  total_attendu: number;
  total_recu: number;
  nb_payes: number;
  nb_retards: number;
  nb_attente: number;
};

/** Clé de rendu : les lignes externes et vacantes n'ont pas de bail. */
function rowKey(r: Row): string {
  if (r.etat === "vacant") return `vac-${r.logement_id}`;
  return r.gestion_externe
    ? `ext-${r.logement_id}`
    : `bail-${r.bail_id}`;
}

/** Identifiant « ligne occupée » : bail_id en interne, logement en
 *  négatif en externe — les deux espaces d'ID ne se marchent pas dessus. */
function busyKey(r: Row): number {
  return r.gestion_externe ? -(r.logement_id ?? 0) : r.bail_id;
}

function externeToRow(x: RowExterne): Row {
  return {
    bail_id: 0,
    immeuble_id: x.immeuble_id,
    immeuble_name: x.immeuble_name,
    logement_id: x.logement_id,
    logement_numero: x.logement_numero,
    locataire_id: null,
    // Nom facultatif saisi sur le logement (2026-09-09) — repère pour
    // le rapport mensuel, jamais de courriel (pas de relance).
    locataire_name: x.locataire_nom ?? null,
    locataire_phone: null,
    locataire_email: null,
    libre_le: null,
    loyer_mensuel: x.loyer_mensuel,
    jour_echeance: 1,
    paiement_id: null,
    montant_paye: x.montant_paye,
    paye_le: x.paye_le,
    etat: x.etat,
    frais_mois: [],
    solde_total: x.solde_total,
    solde_anterieur: x.solde_anterieur ?? false,
    nb_relances: 0,
    derniere_relance_le: null,
    gestion_externe: true
  };
}

type Overview = {
  mois: string;
  rows: Row[];
  total_attendu: number;
  total_recu: number;
  nb_payes: number;
  nb_retards: number;
  nb_attente: number;
  total_solde_du?: number;
  //: Démarrage du pôle : les soldes ne remontent pas avant (réglable
  //: dans Paramètres → Gestion locative).
  solde_depuis?: string | null;
};

const RELOC_LABEL: Record<string, string> = {
  bail_a_envoyer: "bail à envoyer",
  bail_envoye: "bail envoyé — à signer",
  a_venir: "à venir"
};

function fmtMoney(n: number): string {
  return `${Math.round(n).toLocaleString("fr-CA")} $`;
}

function monthLabel(mois: string): string {
  const [y, m] = mois.split("-").map(Number);
  return new Date(y, (m || 1) - 1, 1).toLocaleDateString("fr-CA", {
    month: "long",
    year: "numeric"
  });
}

/** "2026-07-01" → "juillet 2026" (date de démarrage du pôle). */
function moisLabelCourt(iso: string): string {
  return monthLabel(iso.slice(0, 7));
}

function shiftMonth(mois: string, delta: number): string {
  const [y, m] = mois.split("-").map(Number);
  const d = new Date(y, (m || 1) - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function currentMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

export default function PaiementsPage() {
  const { currentEntrepriseId } = useImmobilierLayout();
  const [mois, setMois] = useState(currentMonth());
  const [data, setData] = useState<Overview | null>(null);
  const [externe, setExterne] = useState<OverviewExterne | null>(null);
  // 2e validation (banque via QuickBooks) — null quand la feature est
  // inactive ou l'appel échoue : AUCUNE pastille, zéro bruit.
  const [valBanque, setValBanque] = useState<ValidationEtat | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [payingId, setPayingId] = useState<number | null>(null);
  const [relancingId, setRelancingId] = useState<number | null>(null);
  //: Ligne dont la relance attend confirmation (aperçu ouvert).
  const [relanceApercu, setRelanceApercu] = useState<Row | null>(null);
  const [correctingId, setCorrectingId] = useState<number | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [etatFilter, setEtatFilter] = useState<
    "all" | "paye" | "partiel" | "retard" | "attente" | "vacant"
  >("all");
  const [immeubleFilter, setImmeubleFilter] = useState<number | "all">("all");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ mois });
      if (currentEntrepriseId != null) {
        params.set("entreprise_id", String(currentEntrepriseId));
      }
      // Deux sources : le suivi interne (baux) et le miroir des
      // immeubles en gestion externe (par logement). Un échec côté
      // externe ne doit pas masquer le tableau principal.
      const [r, rx] = await Promise.all([
        authedFetch(
          `/api/v1/immobilier/loyers/overview?${params.toString()}`
        ),
        authedFetch(
          `/api/v1/immobilier/loyers/externes?${params.toString()}`
        )
      ]);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData((await r.json()) as Overview);
      setExterne(rx.ok ? ((await rx.json()) as OverviewExterne) : null);
      // 2e validation bancaire (fail-quiet : null = aucune pastille).
      setValBanque(await chargerValidationBancaire(mois));
    } catch (e) {
      setError(`Chargement échoué : ${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [mois, currentEntrepriseId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Changement d'entreprise → le portefeuille change, on repart sur « Tous ».
  useEffect(() => {
    setImmeubleFilter("all");
  }, [currentEntrepriseId]);

  function flash(msg: string) {
    setToast(msg);
    window.setTimeout(() => setToast(null), 2500);
  }

  async function enregistrerPaiement(row: Row, montant: number) {
    setPayingId(row.bail_id);
    try {
      const today = new Date();
      const payeLe = `${today.getFullYear()}-${String(
        today.getMonth() + 1
      ).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
      const r = await authedFetch("/api/v1/immobilier/paiements", {
        method: "POST",
        body: JSON.stringify({
          bail_id: row.bail_id,
          // Ligne « dette » d'un bail terminé : imputer au dernier mois
          // couvert (le backend refuse un mois hors période du bail).
          mois_couvert: `${moisCouvertPourPaiement(row, mois)}-01`,
          montant,
          paye_le: payeLe
        })
      });
      if (!r.ok) {
        const t = await r.text();
        throw new Error(t.slice(0, 200) || `HTTP ${r.status}`);
      }
      flash(
        `Paiement enregistré — ${row.locataire_name || "locataire"} (${fmtMoney(
          montant
        )})`
      );
      await load();
    } catch (e) {
      setError(`Paiement échoué : ${(e as Error).message}`);
    } finally {
      setPayingId(null);
    }
  }

  // Payé AU COMPLET en 1 clic : le restant du mois (loyer + frais −
  // déjà payé) — et le SOLDE du bail pour une ligne « dette seulement »
  // (bail terminé, mois après sa fin — retour client 2026-08-14).
  async function marquerPaye(row: Row) {
    await enregistrerPaiement(row, montantMarquerPaye(row));
  }

  // Paiement PARTIEL : montant saisi (ex. 500 $ sur un loyer de 800 $).
  async function marquerPartiel(row: Row) {
    const restant =
      Math.round((duMois(row) - (row.montant_paye ?? 0)) * 100) / 100;
    const saisie = window.prompt(
      `Montant reçu de ${row.locataire_name || "ce locataire"} pour ${mois} ?\n(Restant du mois : ${fmtMoney(restant)})`,
      ""
    );
    if (saisie == null) return;
    const montant = Number(saisie.replace(/\s/g, "").replace(",", "."));
    if (!Number.isFinite(montant) || montant <= 0) {
      setError("Montant invalide.");
      return;
    }
    await enregistrerPaiement(row, Math.round(montant * 100) / 100);
  }

  // Frais ponctuel qui S'AJOUTE au solde (ex. 20 $ payé après le 15).
  async function ajouterFrais(row: Row) {
    const saisie = window.prompt(
      `Frais ou crédit pour ${row.locataire_name || "ce locataire"} (mois ${mois}) ?\n` +
        "Montant en $ — positif = frais (ex. 20), NÉGATIF = crédit qui " +
        "réduit le loyer dû (ex. -50) :",
      "20"
    );
    if (saisie == null) return;
    const montant = Number(saisie.replace(/\s/g, "").replace(",", "."));
    if (!Number.isFinite(montant) || montant === 0) {
      setError("Montant invalide (positif = frais, négatif = crédit).");
      return;
    }
    const defLibelle = montant < 0 ? "Crédit" : "Frais de retard";
    const libelle =
      window.prompt("Libellé :", defLibelle) || defLibelle;
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/baux/${row.bail_id}/frais`,
        {
          method: "POST",
          body: JSON.stringify({
            mois_couvert: `${mois}-01`,
            montant,
            libelle
          })
        }
      );
      if (!r.ok)
        throw new Error((await r.text()).slice(0, 200) || `HTTP ${r.status}`);
      flash(
        montant < 0
          ? `Crédit appliqué au solde : ${libelle} (${fmtMoney(montant)})`
          : `Frais ajouté au solde : ${libelle} (${fmtMoney(montant)})`
      );
      await load();
    } catch (e) {
      setError(`Ajout du frais échoué : ${(e as Error).message}`);
    }
  }

  async function toggleTal(row: Row) {
    const ouvre = !row.tal_dossier_ouvert_le;
    if (
      !ouvre &&
      !window.confirm(
        "Retirer le suivi « dossier TAL ouvert » sur ce bail ?"
      )
    )
      return;
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/baux/${row.bail_id}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            tal_dossier_ouvert_le: ouvre
              ? new Date().toISOString().slice(0, 10)
              : null
          })
        }
      );
      if (!r.ok)
        throw new Error((await r.text()).slice(0, 200) || `HTTP ${r.status}`);
      flash(
        ouvre
          ? "Dossier TAL marqué ouvert — visible par toute l'équipe."
          : "Suivi TAL retiré."
      );
      await load();
    } catch (e) {
      setError(`Mise à jour TAL échouée : ${(e as Error).message}`);
    }
  }

  async function supprimerFrais(fraisId: number) {
    if (!window.confirm("Retirer ce frais du solde ?")) return;
    try {
      const r = await authedFetch(`/api/v1/immobilier/frais/${fraisId}`, {
        method: "DELETE"
      });
      if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
      await load();
    } catch (e) {
      setError(`Suppression du frais échouée : ${(e as Error).message}`);
    }
  }

  // « Corriger » ouvre les OPTIONS (retour Phil 2026-07-31) : corriger
  // le montant, marquer payé au complet, ou retirer — sans perdre la
  // ligne ni devoir la retrouver.
  async function supprimerPaiementsMois(row: Row): Promise<void> {
    const r = await authedFetch(
      `/api/v1/immobilier/baux/${row.bail_id}/paiements-mois?mois=${mois}`,
      { method: "DELETE" }
    );
    if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
  }

  async function retirerPaiement(row: Row) {
    if (
      !window.confirm(
        `Retirer le paiement de ${row.locataire_name || "ce locataire"} pour ${mois} (${fmtMoney(row.montant_paye ?? 0)} reçu) ?\nLe mois redeviendra impayé.`
      )
    )
      return;
    setPayingId(row.bail_id);
    try {
      await supprimerPaiementsMois(row);
      flash("Paiement retiré — le mois est de retour impayé.");
      setCorrectingId(null);
      await load();
    } catch (e) {
      setError(`Annulation échouée : ${(e as Error).message}`);
    } finally {
      setPayingId(null);
    }
  }

  async function corrigerMontant(row: Row) {
    const saisie = window.prompt(
      `Montant RÉELLEMENT reçu de ${row.locataire_name || "ce locataire"} pour ${mois} ?\n(Enregistré : ${fmtMoney(row.montant_paye ?? 0)} — dû du mois : ${fmtMoney(duMois(row))})`,
      ""
    );
    if (saisie == null) return;
    const montant = Number(saisie.replace(/\s/g, "").replace(",", "."));
    if (!Number.isFinite(montant) || montant < 0) {
      setError("Montant invalide.");
      return;
    }
    setPayingId(row.bail_id);
    try {
      await supprimerPaiementsMois(row);
      setCorrectingId(null);
      if (montant > 0) {
        await enregistrerPaiement(row, Math.round(montant * 100) / 100);
      } else {
        flash("Paiements du mois retirés.");
        await load();
        setPayingId(null);
      }
    } catch (e) {
      setError(`Correction échouée : ${(e as Error).message}`);
      setPayingId(null);
    }
  }

  async function payeAuComplet(row: Row) {
    if (
      !window.confirm(
        `Remplacer par un paiement COMPLET (${fmtMoney(duMois(row))}) pour ${mois} ?`
      )
    )
      return;
    setPayingId(row.bail_id);
    try {
      await supprimerPaiementsMois(row);
      setCorrectingId(null);
      await enregistrerPaiement(row, duMois(row));
    } catch (e) {
      setError(`Correction échouée : ${(e as Error).message}`);
      setPayingId(null);
    }
  }

  //: Le bouton ouvre un aperçu — ce rappel n'avait AUCUNE
  //: confirmation, un clic de travers relançait un locataire à jour
  //: (retour Phil 2026-08-19).
  async function relancer(row: Row) {
    setRelanceApercu(row);
  }

  async function envoyerRelance(row: Row) {
    setRelancingId(row.bail_id);
    try {
      const r = await authedFetch("/api/v1/immobilier/loyers/relance", {
        method: "POST",
        body: JSON.stringify({ bail_id: row.bail_id, mois })
      });
      if (!r.ok) {
        const t = await r.text();
        throw new Error(t.slice(0, 200) || `HTTP ${r.status}`);
      }
      const res = (await r.json()) as { niveau: number; destinataire: string };
      flash(
        `Relance ${res.niveau} envoyée à ${res.destinataire}`
      );
      setRelanceApercu(null);
      await load();
    } catch (e) {
      setError(`Relance échouée : ${(e as Error).message}`);
    } finally {
      setRelancingId(null);
    }
  }

  // ── Gestion externe : les mêmes gestes, sur le logement ────────────
  // Pas de bail ni de locataire chez nous : on coche ce que le rapport
  // du gestionnaire indique. Ni relance ni frais ponctuel ici.
  async function enregistrerExterne(row: Row, montant: number) {
    if (row.logement_id == null) return;
    setPayingId(busyKey(row));
    try {
      const r = await authedFetch("/api/v1/immobilier/paiements-externes", {
        method: "POST",
        body: JSON.stringify({
          logement_id: row.logement_id,
          mois,
          montant
        })
      });
      if (!r.ok)
        throw new Error((await r.text()).slice(0, 200) || `HTTP ${r.status}`);
      flash(
        `Paiement enregistré — ${row.immeuble_name} · ${row.logement_numero} (${fmtMoney(montant)})`
      );
      await load();
    } catch (e) {
      setError(`Paiement échoué : ${(e as Error).message}`);
    } finally {
      setPayingId(null);
    }
  }

  async function marquerPayeExterne(row: Row) {
    // 1 clic = tout ce qui est dû (restant du mois + mois antérieurs —
    // solde cumulatif, retour Phil 2026-09-09), sinon le loyer du mois.
    const restant =
      Math.round((row.loyer_mensuel - (row.montant_paye ?? 0)) * 100) / 100;
    const solde = row.solde_total ?? 0;
    await enregistrerExterne(
      row,
      solde > 0 ? solde : restant > 0 ? restant : row.loyer_mensuel
    );
  }

  async function renommerExterne(row: Row) {
    if (row.logement_id == null) return;
    const v = window.prompt(
      `Nom du locataire pour ${row.immeuble_name} · ${row.logement_numero} (vide = aucun)`,
      row.locataire_name ?? ""
    );
    if (v === null) return;
    const r = await authedFetch(
      `/api/v1/immobilier/logements/${row.logement_id}`,
      {
        method: "PATCH",
        body: JSON.stringify({ locataire_externe_nom: v.trim() || null })
      }
    );
    if (!r.ok) setError("Nom non enregistré.");
    await load();
  }

  async function marquerPartielExterne(row: Row) {
    const restant =
      Math.round((row.loyer_mensuel - (row.montant_paye ?? 0)) * 100) / 100;
    const saisie = window.prompt(
      `Montant reçu pour le logement ${row.logement_numero} (${mois}) ?\n(Restant du mois : ${fmtMoney(restant)})`,
      ""
    );
    if (saisie == null) return;
    const montant = Number(saisie.replace(/\s/g, "").replace(",", "."));
    if (!Number.isFinite(montant) || montant <= 0) {
      setError("Montant invalide.");
      return;
    }
    await enregistrerExterne(row, Math.round(montant * 100) / 100);
  }

  async function corrigerExterne(row: Row) {
    if (row.logement_id == null) return;
    if (
      !window.confirm(
        `SUPPRIMER les paiements saisis du logement ${row.logement_numero} pour ${mois} ?

Le mois redeviendra impayé — cette action ne se défait pas.`
      )
    )
      return;
    setPayingId(busyKey(row));
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/paiements-externes/${row.logement_id}?mois=${mois}`,
        { method: "DELETE" }
      );
      if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
      flash("Paiement retiré — le mois est de retour impayé.");
      await load();
    } catch (e) {
      setError(`Annulation échouée : ${(e as Error).message}`);
    } finally {
      setPayingId(null);
    }
  }

  // Toutes les lignes du mois : interne + gestion externe.
  const allRows = useMemo(() => {
    const internes = data?.rows ?? [];
    const externes = (externe?.rows ?? []).map(externeToRow);
    return [...internes, ...externes];
  }, [data, externe]);

  // Immeubles distincts présents dans les rows du mois chargé.
  const immeubleOptions = useMemo(() => {
    const m = new Map<number, string>();
    for (const r of allRows) {
      if (!m.has(r.immeuble_id)) m.set(r.immeuble_id, r.immeuble_name);
    }
    return [...m.entries()]
      .map(([id, name]) => ({ id, name }))
      .sort((a, b) => a.name.localeCompare(b.name, "fr"));
  }, [allRows]);

  // Tuiles KPI : suivent le filtre immeuble (voir l'état d'UN immeuble),
  // mais ignorent le filtre état + la recherche. Les totaux du backend
  // ne couvrent que l'interne → on ajoute le portefeuille externe.
  const kpi = useMemo(() => {
    if (!data) return null;
    if (immeubleFilter === "all") {
      return {
        total_attendu: data.total_attendu + (externe?.total_attendu ?? 0),
        total_recu: data.total_recu + (externe?.total_recu ?? 0),
        nb_retards: data.nb_retards + (externe?.nb_retards ?? 0),
        nb_attente: data.nb_attente + (externe?.nb_attente ?? 0),
        nb_vacants: allRows.filter((r) => r.etat === "vacant").length,
        nb_baux: allRows.filter((r) => r.etat !== "vacant").length,
        total_solde_du:
          (data.total_solde_du ??
            data.rows.reduce((s, r) => s + (r.solde_total ?? 0), 0)) +
          (externe?.rows ?? []).reduce((s, r) => s + r.solde_total, 0)
      };
    }
    const rows = allRows.filter((r) => r.immeuble_id === immeubleFilter);
    // Les lignes vacantes sont informatives : leur « loyer demandé »
    // n'entre dans aucun total d'argent.
    const actives = rows.filter((r) => r.etat !== "vacant");
    return {
      total_attendu: actives.reduce((s, r) => s + r.loyer_mensuel, 0),
      total_recu: actives.reduce((s, r) => s + (r.montant_paye ?? 0), 0),
      nb_retards: actives.filter(
        (r) => r.etat === "retard" || r.etat === "partiel"
      ).length,
      nb_attente: actives.filter((r) => r.etat === "attente").length,
      nb_vacants: rows.filter((r) => r.etat === "vacant").length,
      nb_baux: actives.length,
      total_solde_du: actives.reduce((s, r) => s + (r.solde_total ?? 0), 0)
    };
  }, [data, externe, allRows, immeubleFilter]);

  const tauxCollecte = useMemo(() => {
    if (!kpi || kpi.total_attendu <= 0) return null;
    return Math.round((kpi.total_recu / kpi.total_attendu) * 100);
  }, [kpi]);

  // Filtres client-side sur les rows du mois chargé + tri par état :
  // retards en haut, partiels ensuite, payés en bas (retour Steven).
  const filteredRows = useMemo(() => {
    if (!data) return [];
    const q = search.trim().toLowerCase();
    const ordre: Record<string, number> = {
      retard: 0,
      partiel: 1,
      attente: 2,
      paye: 3
    };
    return allRows
      .filter((r) => {
        if (immeubleFilter !== "all" && r.immeuble_id !== immeubleFilter)
          return false;
        if (etatFilter !== "all" && r.etat !== etatFilter) return false;
        if (q) {
          // « gestion externe » est cherchable comme un nom de locataire :
          // c'est ce que la ligne affiche à sa place.
          const hay = `${r.locataire_name || ""} ${r.immeuble_name} ${
            r.logement_numero || ""
          }${r.gestion_externe ? " gestion externe" : ""}${
            r.etat === "vacant" ? " vacant" : ""
          }`.toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      })
      .sort(
        (a, b) =>
          (ordre[a.etat] ?? 9) - (ordre[b.etat] ?? 9) ||
          a.immeuble_name.localeCompare(b.immeuble_name, "fr") ||
          (a.logement_numero || "").localeCompare(
            b.logement_numero || "", "fr"
          )
      );
  }, [data, allRows, search, etatFilter, immeubleFilter]);

  // Pastilles ✓✓ / ⚠ de la 2e validation, indexées par bail.
  const valMap = useMemo(() => indexValidations(valBanque), [valBanque]);

  return (
    <>
      <ImmobilierTopbar
        breadcrumbs={[
          { label: "Gestion immobilière", href: "/immobilier" },
          { label: "Paiements" }
        ]}
      />
      <div className="p-4 pb-28 lg:p-6 lg:pb-28">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-start gap-3">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent-500/15 text-accent-500">
              <ClipboardList className="h-5 w-5" />
            </span>
            <div>
              <h1 className="text-2xl font-bold text-white">Paiements</h1>
              <p className="mt-1 max-w-2xl text-sm text-white/60">
                Tous les baux actifs du portefeuille — qui a payé, qui est
                en retard, et marquer payé en un clic.
              </p>
            </div>
          </div>

          {/* Crochet fil bancaire (validation QuickBooks) + sélecteur de mois */}
          <div className="flex items-center gap-2">
            <CrochetFilBancaire
              actif={!!valBanque?.active}
              onChange={() => void load()}
            />
            <div className="inline-flex items-center gap-1 rounded-lg border border-brand-800 bg-brand-900 px-1 py-1">
              <button
                type="button"
                onClick={() => setMois((m) => shiftMonth(m, -1))}
                className="btn-ghost btn-xs"
                aria-label="Mois précédent"
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
              <span className="min-w-[140px] text-center text-sm font-semibold capitalize text-white">
                {monthLabel(mois)}
              </span>
              <button
                type="button"
                onClick={() => setMois((m) => shiftMonth(m, 1))}
                className="btn-ghost btn-xs"
                aria-label="Mois suivant"
              >
                <ChevronRight className="h-4 w-4" />
              </button>
            </div>
            {/* Export CSV/Excel : le mois affiché (mêmes lignes, mêmes
                états que le tableau, filtre immeuble compris) ou une
                période du/au. */}
            <BoutonExport
              cibles={[
                {
                  label: `Mois affiché (${monthLabel(mois)})`,
                  base: "/api/v1/immobilier/exports/paiements",
                  sujet: `paiements_${mois}`,
                  params: {
                    mois,
                    immeuble_id:
                      immeubleFilter !== "all" ? immeubleFilter : undefined,
                    entreprise_id: currentEntrepriseId ?? undefined
                  }
                }
              ]}
              periode={{
                base: "/api/v1/immobilier/exports/paiements",
                sujet: "paiements_periode",
                du: shiftMonth(mois, -11),
                au: mois,
                params: {
                  immeuble_id:
                    immeubleFilter !== "all" ? immeubleFilter : undefined,
                  entreprise_id: currentEntrepriseId ?? undefined
                }
              }}
            />
          </div>
        </header>

        {error ? (
          <p className="mt-4 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
            {error}
          </p>
        ) : null}

        {/* Tuiles de synthèse — suivent le filtre immeuble */}
        {kpi ? (
          <div className="mt-5 grid grid-cols-2 gap-3 lg:grid-cols-6">
            <StatTile
              label="Attendu"
              value={fmtMoney(kpi.total_attendu)}
              sub={`${kpi.nb_baux} bail${kpi.nb_baux > 1 ? "s" : ""} actif${kpi.nb_baux > 1 ? "s" : ""}`}
            />
            <StatTile
              label="Reçu"
              value={fmtMoney(kpi.total_recu)}
              sub={
                tauxCollecte != null
                  ? `${tauxCollecte} % collecté`
                  : undefined
              }
              tone="emerald"
            />
            <StatTile
              label="En retard"
              value={String(kpi.nb_retards)}
              sub={kpi.nb_retards > 0 ? "à relancer 👇" : "rien à signaler"}
              tone={kpi.nb_retards > 0 ? "rose" : undefined}
            />
            <StatTile
              label="En attente"
              value={String(kpi.nb_attente)}
              sub="avant le 5 du mois"
            />
            <StatTile
              label="Solde dû"
              value={fmtMoney(kpi.total_solde_du)}
              sub={
                data?.solde_depuis
                  ? `depuis ${moisLabelCourt(data.solde_depuis)}`
                  : "loyers échus + frais − reçus"
              }
              tone={kpi.total_solde_du > 0 ? "rose" : undefined}
            />
            <StatTile
              label="Vacants"
              value={String(kpi.nb_vacants)}
              sub={
                kpi.nb_vacants > 0
                  ? "aucun loyer ce mois-ci"
                  : "tout est loué"
              }
              tone={kpi.nb_vacants > 0 ? "amber" : undefined}
            />
          </div>
        ) : null}

        {relanceApercu ? (
          <ApercuEnvoiModal
            titre="Relance de loyer"
            description={
              `Un rappel de loyer pour ${mois}. Le niveau s'incrémente ` +
              "à chaque relance du même mois — ces envois servent de " +
              "preuve au TAL, donc vérifie que le loyer est bien impayé."
            }
            destinataireNom={relanceApercu.locataire_name || "Locataire"}
            destinataireEmail={relanceApercu.locataire_email}
            libelleEnvoi="Envoyer la relance"
            busy={relancingId === relanceApercu.bail_id}
            onAnnuler={() => setRelanceApercu(null)}
            onConfirmer={() => void envoyerRelance(relanceApercu)}
          />
        ) : null}
        <BandeauAvisRenouvellement entrepriseId={currentEntrepriseId} />
        <BandeauBailManquant entrepriseId={currentEntrepriseId} />
        <BandeauDepotARembourser entrepriseId={currentEntrepriseId} />

        {/* Filtres */}
        <div className="mt-5 flex flex-wrap items-center gap-2">
          <div className="relative max-w-md flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/40" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Recherche locataire / immeuble…"
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
            aria-label="Filtrer par immeuble"
          >
            <option value="all">Tous les immeubles</option>
            {immeubleOptions.map((imm) => (
              <option key={imm.id} value={imm.id}>
                {imm.name}
              </option>
            ))}
          </select>
          <FilterPill
            label="Tous"
            active={etatFilter === "all"}
            onClick={() => setEtatFilter("all")}
          />
          <FilterPill
            label="Payés"
            active={etatFilter === "paye"}
            onClick={() => setEtatFilter("paye")}
          />
          <FilterPill
            label="Retards"
            active={etatFilter === "retard"}
            onClick={() => setEtatFilter("retard")}
          />
          <FilterPill
            label="Partiels"
            active={etatFilter === "partiel"}
            onClick={() => setEtatFilter("partiel")}
          />
          <FilterPill
            label="En attente"
            active={etatFilter === "attente"}
            onClick={() => setEtatFilter("attente")}
          />
          <FilterPill
            label="Vacants"
            active={etatFilter === "vacant"}
            onClick={() => setEtatFilter("vacant")}
          />
          {data ? (
            <span className="text-xs text-white/50">
              {filteredRows.length} / {allRows.length}
            </span>
          ) : null}
        </div>

        {/* Tableau */}
        <div className="mt-4 overflow-hidden rounded-xl border border-brand-800 bg-brand-900">
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-accent-500" />
            </div>
          ) : !data || filteredRows.length === 0 ? (
            <div className="p-10 text-center text-sm text-white/50">
              {allRows.length > 0 ? (
                "Aucun bail correspondant aux filtres."
              ) : (
                <>
                  Aucun bail actif dans le portefeuille
                  {currentEntrepriseId != null ? " de cette entreprise" : ""}.
                  Crée des baux depuis les fiches immeubles.
                </>
              )}
            </div>
          ) : (
            // min-w : sur mobile la table défile horizontalement plutôt que
            // de compresser la colonne d'actions (sinon les boutons s'empilent
            // et les lignes deviennent très hautes — retour Phil v4).
            <div className="overflow-x-auto">
              <table className="w-full min-w-[1180px] text-sm">
                <thead className="bg-brand-950/60 text-left text-[11px] uppercase tracking-wider text-white/50">
                  <tr>
                    <th className="px-3 py-2.5">État</th>
                    <th className="px-3 py-2.5">Locataire</th>
                    <th className="px-3 py-2.5">Immeuble · log.</th>
                    <th className="px-3 py-2.5">Statut</th>
                    {/* Loyer / Reçu / Solde tiennent dans UNE colonne
                        (retour Phil 2026-08-13) — plus de « Solde dû »
                        séparé, on lit la ligne d'un coup d'œil. */}
                    <th className="px-3 py-2.5 text-right">Loyer</th>
                    <th className="px-3 py-2.5 text-right">Payé le</th>
                    <th className="px-3 py-2.5"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-brand-800">
                  {filteredRows.map((r) => (
                    <tr
                      // `rowKey` et pas `bail_id` : en gestion externe la
                      // ligne suit le LOGEMENT, il n'y a pas de bail chez nous.
                      key={rowKey(r)}
                      // Fond TEINTÉ selon l'état (retour Phil
                      // 2026-08-13) : gris en attente, vert payé,
                      // jaune partiel, rouge retard — mêmes teintes
                      // que les lignes de résiliation de la page Baux.
                      className={`transition ${
                        r.etat === "retard"
                          ? "bg-rose-500/10 hover:bg-rose-500/15"
                          : r.etat === "partiel"
                            ? "bg-amber-500/10 hover:bg-amber-500/15"
                            : r.etat === "paye"
                              ? "bg-emerald-500/10 hover:bg-emerald-500/15"
                              : "hover:bg-brand-800/40"
                      }`}
                    >
                      <td className="px-3 py-2.5">
                        {r.etat === "paye" ? (
                          <span className="badge badge-emerald">
                            <CheckCircle2 className="h-3 w-3" /> Payé
                          </span>
                        ) : r.etat === "partiel" ? (
                          <span className="badge badge-amber">
                            <AlertTriangle className="h-3 w-3" />{" "}
                            {r.solde_anterieur
                              ? "Solde antérieur"
                              : "Partiel"}
                          </span>
                        ) : r.etat === "retard" ? (
                          <span className="badge badge-rose">
                            <AlertTriangle className="h-3 w-3" /> Retard
                          </span>
                        ) : r.etat === "vacant" ? (
                          <span
                            className="badge badge-amber"
                            title={
                              r.logement_statut === "reserve"
                                ? "Bail signé, pas encore commencé — aucun loyer ce mois-ci"
                                : "Aucun bail ne couvre ce mois — aucun loyer à percevoir"
                            }
                          >
                            <KeyRound className="h-3 w-3" />{" "}
                            {r.logement_statut === "reserve"
                              ? "Réservé"
                              : "Vacant"}
                          </span>
                        ) : (
                          <span className="badge badge-neutral">
                            <Clock className="h-3 w-3" /> Attente
                          </span>
                        )}
                        {/* 2e validation (banque via QuickBooks) —
                            pastille discrète, absente quand la feature
                            est inactive ou l'immeuble non mappé. */}
                        {!r.gestion_externe && valMap.get(r.bail_id) ? (
                          <div className="mt-1">
                            <PastilleValidationBancaire
                              v={valMap.get(r.bail_id)}
                              bailId={r.bail_id}
                              mois={`${mois}-01`}
                              onChange={() => void load()}
                            />
                          </div>
                        ) : null}
                      </td>
                      <td className="px-3 py-2.5">
                        {r.etat === "vacant" ? (
                          <span className="text-sm italic text-white/40">
                            Aucun locataire
                          </span>
                        ) : r.gestion_externe ? (
                          <BadgeGestionExterne
                            nom={r.locataire_name}
                            onRename={() => void renommerExterne(r)}
                          />
                        ) : r.locataire_id != null ? (
                          <Link
                            // eslint-disable-next-line @typescript-eslint/no-explicit-any
                            href={
                              `/immobilier/locataires/${r.locataire_id}` as any
                            }
                            className="font-medium text-accent-500 hover:underline"
                            title="Ouvrir la fiche du locataire"
                          >
                            {r.locataire_name || `Locataire #${r.locataire_id}`}
                          </Link>
                        ) : (
                          <span className="font-medium text-white">
                            {r.locataire_name || "—"}
                          </span>
                        )}
                        {/* Un départ acté change la lecture du retard :
                            inutile de relancer pour un mois qu'il
                            n'occupera pas (retour Phil 2026-08-19). */}
                        {r.libre_le ? (
                          <span
                            className="ml-2 inline-flex items-center rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-amber-300"
                            title={`Départ confirmé — le logement se libère le ${r.libre_le}`}
                          >
                            part le{" "}
                            {(() => {
                              const [y, m, d] = r.libre_le
                                .split("-")
                                .map(Number);
                              // Année affichée dès qu'elle diffère de
                              // l'année courante (« 30 juin 2027 », pas
                              // « 30 juin » — retour Phil 2026-09-09).
                              return new Date(
                                y,
                                (m || 1) - 1,
                                d || 1
                              ).toLocaleDateString("fr-CA", {
                                day: "numeric",
                                month: "short",
                                ...(y !== new Date().getFullYear()
                                  ? { year: "numeric" }
                                  : {})
                              });
                            })()}
                          </span>
                        ) : null}
                        {r.locataire_phone ? (
                          <a
                            href={`tel:${r.locataire_phone}`}
                            className="ml-2 inline-flex items-center gap-1 text-[11px] text-accent-500 hover:underline"
                          >
                            <Phone className="h-3 w-3" />
                            {r.locataire_phone}
                          </a>
                        ) : null}
                        {/* Dossier au TAL : visible par toute l'équipe,
                            pour que le suivi ne repose plus sur du
                            bouche-à-oreille (retour Phil 2026-08-31). */}
                        {r.tal_dossier_ouvert_le ? (
                          <span
                            className="ml-2 inline-flex items-center gap-1 rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-violet-300"
                            title={`Dossier ouvert au TAL le ${r.tal_dossier_ouvert_le} — décochable via le bouton TAL de la ligne`}
                          >
                            <Scale className="h-3 w-3" /> TAL ouvert
                          </span>
                        ) : null}
                      </td>
                      <td className="px-3 py-2.5 text-white/70">
                        <Link
                          // eslint-disable-next-line @typescript-eslint/no-explicit-any
                          href={`/immobilier/immeubles/${r.immeuble_id}` as any}
                          className="hover:text-accent-500 hover:underline"
                        >
                          {r.immeuble_name}
                        </Link>
                        {r.logement_numero ? (
                          r.logement_id != null ? (
                            <Link
                              // eslint-disable-next-line @typescript-eslint/no-explicit-any
                              href={
                                `/immobilier/logements/${r.logement_id}` as any
                              }
                              className="text-accent-500/80 hover:text-accent-500 hover:underline"
                              title="Ouvrir la fiche du logement"
                            >
                              {" "}
                              · {r.logement_numero}
                            </Link>
                          ) : (
                            <span className="text-white/40">
                              {" "}
                              · {r.logement_numero}
                            </span>
                          )
                        ) : null}
                        {r.prochain_nom ? (
                          <div className="mt-0.5 text-[10px] font-normal text-orange-300/90">
                            Prochain : {r.prochain_nom}
                            {r.prochain_loyer != null
                              ? ` · ${fmtMoney(r.prochain_loyer)}`
                              : ""}
                            {r.prochain_debut
                              ? ` dès le ${r.prochain_debut}`
                              : ""}
                            {" — "}
                            {RELOC_LABEL[r.prochain_statut || ""] ||
                              "à venir"}
                          </div>
                        ) : null}
                      </td>
                      <td className="px-3 py-2.5">
                        {r.gestion_externe ? (
                          // Pas de bail chez nous en gestion externe : la
                          // ligne suit le LOGEMENT, forcément occupé
                          // puisqu'un loyer y est attendu.
                          <span className="badge badge-neutral">Occupé</span>
                        ) : r.bail_termine_le ? (
                          // M7 : bail résilié/terminé — la ligne reste
                          // dans les mois couverts, et après la fin tant
                          // que le solde n'est pas réglé (2026-08-14).
                          // Une date de fin FUTURE = départ avant terme :
                          // la date contractuelle serait trompeuse.
                          <span
                            className="badge badge-rose"
                            title={
                              r.bail_termine_le >
                              new Date().toLocaleDateString("sv-SE")
                                ? "Locataire parti avant la fin prévue du bail — la ligne reste tant que le solde n'est pas réglé"
                                : r.bail_termine_le.slice(0, 7) < mois
                                  ? "Bail terminé avant ce mois — la ligne reste tant que le solde n'est pas réglé"
                                  : "Le bail couvrait une partie de ce mois — dernier loyer et solde encore dus"
                            }
                          >
                            {r.bail_termine_le >
                            new Date().toLocaleDateString("sv-SE")
                              ? "Bail terminé (avant terme)"
                              : `Bail terminé le ${r.bail_termine_le}`}
                          </span>
                        ) : (
                          <span className="badge badge-emerald">Actif</span>
                        )}
                        {r.prochain_statut ? (
                          <div className="mt-0.5">
                            <span
                              className={`badge ${
                                r.prochain_statut === "bail_envoye"
                                  ? "badge-violet"
                                  : "badge-amber"
                              }`}
                            >
                              {RELOC_LABEL[r.prochain_statut] || "proposé"}
                            </span>
                          </div>
                        ) : null}
                      </td>
                      <td className="px-3 py-2.5">
                        {/* Bail TAL « Ou le ___ » : rien quand c'est le 1er
                            (l'immense majorité), mention discrète sinon —
                            c'est ce qui explique qu'il ne soit pas encore
                            « en retard ». */}
                        {/* Loyer / Reçu / Solde empilés (retour Phil
                            2026-08-13) — la balance d'un paiement partiel
                            se lit sur la ligne « Solde », plus besoin du
                            « reste » séparé ni d'une colonne Solde dû. */}
                        {r.etat === "vacant" ? (
                          <span
                            className="text-sm tabular-nums text-white/40"
                            title="Loyer demandé (fiche du logement) — rien à percevoir tant que le logement n'est pas loué"
                          >
                            {r.loyer_mensuel > 0
                              ? `${fmtMoney(r.loyer_mensuel)} demandé`
                              : "—"}
                          </span>
                        ) : (
                          <CelluleLoyer
                            loyer={r.loyer_mensuel}
                            recu={r.montant_paye}
                            solde={r.solde_total}
                            fmt={fmtMoney}
                            echeance={echeanceLabel(r.jour_echeance)}
                            frais={r.frais_mois}
                            onSupprimerFrais={(id) => void supprimerFrais(id)}
                          />
                        )}
                      </td>
                      {/* Le montant reçu vit maintenant dans la colonne
                          Loyer — ici on ne garde que la DATE. */}
                      <td className="px-3 py-2.5 text-right tabular-nums text-white/60">
                        {r.paye_le || "—"}
                      </td>
                      <td className="px-3 py-2.5 text-right">
                        {r.etat === "vacant" ? (
                          <Link
                            // eslint-disable-next-line @typescript-eslint/no-explicit-any
                            href={"/immobilier/locations" as any}
                            className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs font-semibold text-amber-300 transition hover:bg-amber-500/20"
                            title="Ouvrir le pipeline de relocation (annonces, visites, candidat)"
                          >
                            <KeyRound className="h-3 w-3" /> Relouer
                          </Link>
                        ) : (
                        <div className="flex flex-col items-end gap-1.5">
                          <div className="flex flex-wrap items-center justify-end gap-2">
                            {/* ── Groupe 1 — Paiement ── */}
                            <div className="inline-flex items-center gap-1.5 rounded-xl border border-brand-700 px-1.5 py-1">
                              {r.gestion_externe ? (
                                // Gestion externe : on ne fait que refléter
                                // le rapport du gestionnaire — ni relance
                                // (aucun courriel ne part d'ici), ni frais.
                                <>
                                  {r.etat !== "paye" ? (
                                    <>
                                      <button
                                        type="button"
                                        onClick={() =>
                                          void marquerPayeExterne(r)
                                        }
                                        disabled={payingId === busyKey(r)}
                                        className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 text-xs font-semibold text-emerald-300 transition hover:bg-emerald-500/20 disabled:opacity-50"
                                      >
                                        {payingId === busyKey(r) ? (
                                          <Loader2 className="h-3 w-3 animate-spin" />
                                        ) : (
                                          <Check className="h-3 w-3" />
                                        )}
                                        Marquer payé
                                      </button>
                                      <button
                                        type="button"
                                        onClick={() =>
                                          void marquerPartielExterne(r)
                                        }
                                        disabled={payingId === busyKey(r)}
                                        title="Enregistrer un paiement partiel (montant saisi, ajouté au cumul)"
                                        className="inline-flex items-center gap-1.5 rounded-lg border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 text-xs font-semibold text-sky-300 transition hover:bg-sky-500/20 disabled:opacity-50"
                                      >
                                        Partiel
                                      </button>
                                    </>
                                  ) : null}
                                  {(r.montant_paye ?? 0) > 0 ||
                                  r.etat === "paye" ? (
                                    <button
                                      type="button"
                                      onClick={() => void corrigerExterne(r)}
                                      disabled={payingId === busyKey(r)}
                                      title="Erreur de saisie ? Annule les paiements du mois"
                                      className="px-1 text-[11px] text-white/50 transition hover:text-rose-300 disabled:opacity-50"
                                    >
                                      Corriger
                                    </button>
                                  ) : null}
                                </>
                              ) : r.etat !== "paye" ? (
                                <>
                                  <button
                                    type="button"
                                    onClick={() => void marquerPaye(r)}
                                    disabled={payingId === r.bail_id}
                                    className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 text-xs font-semibold text-emerald-300 transition hover:bg-emerald-500/20 disabled:opacity-50"
                                  >
                                    {payingId === r.bail_id ? (
                                      <Loader2 className="h-3 w-3 animate-spin" />
                                    ) : (
                                      <Check className="h-3 w-3" />
                                    )}
                                    Marquer payé
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => void marquerPartiel(r)}
                                    disabled={payingId === r.bail_id}
                                    title="Enregistrer un paiement partiel (montant saisi)"
                                    className="inline-flex items-center gap-1.5 rounded-lg border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 text-xs font-semibold text-sky-300 transition hover:bg-sky-500/20 disabled:opacity-50"
                                  >
                                    Partiel
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => void ajouterFrais(r)}
                                    title="Ajouter un frais (ex. retard 20 $) ou un crédit (montant négatif — réduit le loyer dû)"
                                    className="inline-flex items-center gap-1.5 rounded-lg border border-white/15 bg-white/5 px-2.5 py-1 text-xs font-semibold text-white/70 transition hover:bg-white/10"
                                  >
                                    ± Frais/crédit
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => void relancer(r)}
                                    disabled={relancingId === r.bail_id}
                                    title="Envoyer un rappel de loyer par courriel au locataire"
                                    className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs font-semibold text-amber-300 transition hover:bg-amber-500/20 disabled:opacity-50"
                                  >
                                    {relancingId === r.bail_id ? (
                                      <Loader2 className="h-3 w-3 animate-spin" />
                                    ) : (
                                      <Mail className="h-3 w-3" />
                                    )}
                                    Relancer
                                  </button>
                                  {(r.montant_paye ?? 0) > 0 ? (
                                    correctingId === r.bail_id ? (
                                      <CorrectionOptions
                                        r={r}
                                        busy={payingId === r.bail_id}
                                        onMontant={() => void corrigerMontant(r)}
                                        onComplet={() => void payeAuComplet(r)}
                                        onRetirer={() => void retirerPaiement(r)}
                                        onClose={() => setCorrectingId(null)}
                                      />
                                    ) : (
                                      <button
                                        type="button"
                                        onClick={() =>
                                          setCorrectingId(r.bail_id)
                                        }
                                        disabled={payingId === r.bail_id}
                                        title="Corriger le paiement : montant, complet, ou retrait"
                                        className="px-1 text-[11px] text-white/40 transition hover:text-rose-300 disabled:opacity-50"
                                      >
                                        Corriger
                                      </button>
                                    )
                                  ) : null}
                                </>
                              ) : correctingId === r.bail_id ? (
                                <CorrectionOptions
                                  r={r}
                                  busy={payingId === r.bail_id}
                                  onMontant={() => void corrigerMontant(r)}
                                  onComplet={() => void payeAuComplet(r)}
                                  onRetirer={() => void retirerPaiement(r)}
                                  onClose={() => setCorrectingId(null)}
                                />
                              ) : (
                                <button
                                  type="button"
                                  onClick={() => setCorrectingId(r.bail_id)}
                                  disabled={payingId === r.bail_id}
                                  title="Corriger le paiement : montant, complet, ou retrait"
                                  className="inline-flex items-center gap-1.5 rounded-lg border border-white/15 bg-white/5 px-2.5 py-1 text-xs font-semibold text-white/60 transition hover:text-rose-300 disabled:opacity-50"
                                >
                                  Corriger
                                </button>
                              )}
                            {/* Dossier TAL : coche partagée pour que
                                Phil n'ait plus à relancer le
                                responsable des paiements — DANS le
                                groupe pour rester sur une seule ligne
                                (retour Phil 2026-09-01). */}
                            {!r.gestion_externe ? (
                              <button
                                type="button"
                                onClick={() => void toggleTal(r)}
                                title={
                                  r.tal_dossier_ouvert_le
                                    ? `Dossier TAL ouvert le ${r.tal_dossier_ouvert_le} — cliquer pour retirer le suivi`
                                    : "Marquer qu'un dossier de non-paiement est ouvert au TAL pour ce bail"
                                }
                                className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-semibold transition ${
                                  r.tal_dossier_ouvert_le
                                    ? "border-violet-500/40 bg-violet-500/10 text-violet-300 hover:bg-violet-500/20"
                                    : "border-white/15 bg-white/5 text-white/50 hover:bg-white/10 hover:text-white/80"
                                }`}
                              >
                                <Scale className="h-3 w-3" />
                                {r.tal_dossier_ouvert_le
                                  ? "TAL ouvert"
                                  : "TAL"}
                              </button>
                            ) : null}
                            </div>
                            {/* Page PAIEMENTS pure (split v15) : les
                                avis et le bail vivent sur la page
                                « Baux » du menu. */}
                          </div>
                          {r.nb_relances > 0 ? (
                            <span className="text-[10px] text-white/40">
                              Relancé {r.nb_relances}×
                              {r.derniere_relance_le
                                ? ` · dernière ${r.derniere_relance_le}`
                                : ""}
                            </span>
                          ) : null}
                        </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Encart replié « Encaissés non marqués » + ambiguës à
            rapprocher (2e validation bancaire). Rien quand la feature
            est inactive ou qu'il n'y a rien à traiter. */}
        <EncartValidationBancaire etat={valBanque} onChange={() => void load()} />

        <p className="mt-3 text-[11px] text-white/40">
          « Retard » = aucun paiement après le 5 du mois · « Partiel » = un
          montant a été reçu mais le mois n&apos;est pas couvert. « Marquer
          payé » enregistre le restant du mois en 1 clic ; « Partiel » saisit
          un montant précis ; « ± Frais/crédit » ajoute un frais ponctuel
          (ex. 20 $ de retard) ou un crédit (montant négatif) qui réduit le
          loyer dû. « TAL » marque qu&apos;un dossier de non-paiement est
          ouvert au tribunal — visible par toute l&apos;équipe. Les lignes
          « Vacant » listent les logements sans bail ce mois-ci (loyer
          demandé à titre indicatif, exclu des totaux). La colonne
          « Loyer » empile le loyer du mois,
          le « Reçu » et le « Solde » : ce dernier cumule tous les loyers
          échus et frais du bail, moins tout ce qui a été reçu. Les lignes
          « Gestion externe » reflètent le rapport de la compagnie de
          gestion — pas de locataire nominatif, pas de relance.
        </p>
      </div>

      {toast ? (
        <div className="pointer-events-none fixed inset-x-0 bottom-4 z-[1100] flex justify-center px-3">
          <div className="pointer-events-auto flex items-center gap-2 rounded-xl border border-emerald-500/40 bg-emerald-500/15 px-3 py-2 text-sm text-emerald-100 shadow-lg">
            <CheckCircle2 className="h-4 w-4" />
            {toast}
          </div>
        </div>
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

function StatTile({
  label,
  value,
  sub,
  tone
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "emerald" | "rose" | "amber";
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
