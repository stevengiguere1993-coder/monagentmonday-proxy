"use client";

import { use, useCallback, useEffect, useState } from "react";
import {
  ArrowLeft,
  Building2,
  Check,
  DoorOpen,
  FileText,
  Loader2,
  Pencil,
  StickyNote,
  Trash2,
  TrendingUp,
  User,
  Wrench,
  X
} from "lucide-react";
import { useSearchParams } from "next/navigation";

import { Link, useRouter } from "@/i18n/navigation";
import { TableauSuiviBaux } from "@/components/immobilier/tableau-suivi-baux";
import { type SuiviBailRow } from "@/components/immobilier/fin-bail";
import { authedFetch } from "@/lib/auth";
import {
  AuMoisToggle,
  ResilierBailButton,
  BailDocActions,
  DocumentsSection
} from "@/components/immobilier/tal-avis";
import { ImmobilierTopbar } from "../../layout";
import { AssignerBailButton } from "@/components/immobilier/assigner-bail";
import {
  echeanceLabel,
  JourEcheanceInline,
  KANBAN_STATUTS,
  LOUER_INDEFINIMENT_INFO,
  LOUER_INDEFINIMENT_LABEL,
  LouerIndefinimentBulle,
  RelocationStatutPastille
} from "@/components/immobilier/fin-bail";
import {
  fmtPieces,
  type LogementFicheData
} from "@/components/immobilier/logement-fiche";
import { CelluleLoyer } from "@/components/immobilier/paiements-actions";

/**
 * Fiche logement — VRAIE page 360 d'un logement : infos (ÉDITABLES
 * directement dans la page — retour Phil 2026-07-10, plus de modale),
 * mini-KPIs, locataire actuel, historique des locataires, fluctuation
 * du loyer, rénos/maintenance, documents et notes. Le bouton retour est
 * contextuel : ?from=immeuble → fiche immeuble onglet Logements, sinon
 * liste des logements.
 */

type DossierLocataire = { id: number; full_name: string };

type DossierBail = {
  id: number;
  locataire: DossierLocataire | null;
  loyer_mensuel: number;
  date_debut: string;
  date_fin: string;
  status: string;
  document_url: string | null;
  signed_at: string | null;
  document_id?: number | null;
  au_mois?: boolean | null;
  /** Jour du mois où le loyer est payable (bail TAL « Ou le ___ »). */
  jour_echeance?: number | null;
  relocation_statut?: string | null;
  /** Dossier de relocation lié — lien ciblé vers le kanban. */
  relocation_dossier_id?: number | null;
};

type DossierBon = {
  id: number;
  reference: string;
  title: string;
  status: string;
  montant: number | null;
  created_at: string | null;
};

type LoyerPoint = { date_debut: string; loyer_mensuel: number };

type Dossier = {
  logement: LogementFicheData;
  immeuble: {
    id: number;
    name: string;
    address: string | null;
    /** Gestion externe : le loyer SAISI sur le logement est la vérité
     *  (retour client 2026-08-14) — l'affichage du loyer s'adapte. */
    gestion_externe?: boolean;
  };
  baux: DossierBail[];
  bons_travail: DossierBon[];
  historique_loyer: LoyerPoint[];
};

const BAIL_STATUS_LABEL: Record<string, string> = {
  actif: "Actif",
  termine: "Terminé",
  resilie: "Résilié",
  propose: "Proposé"
};

const BAIL_STATUS_BADGE: Record<string, string> = {
  actif: "badge-emerald",
  termine: "badge-neutral",
  resilie: "badge-rose",
  propose: "badge-sky"
};

const BON_STATUS_LABEL: Record<string, string> = {
  draft: "Brouillon",
  sent: "Envoyé",
  signed: "Signé",
  accepte_a_planifier: "Accepté à planifier",
  planifie: "Planifié",
  complete_a_refacturer: "Complété · à refacturer",
  facture: "Facturé",
  cancelled: "Annulé"
};

const BON_STATUS_BADGE: Record<string, string> = {
  draft: "badge-neutral",
  sent: "badge-sky",
  signed: "badge-sky",
  accepte_a_planifier: "badge-amber",
  planifie: "badge-blue",
  complete_a_refacturer: "badge-violet",
  facture: "badge-emerald",
  cancelled: "badge-neutral"
};

function money(n: number | null | undefined): string {
  if (n == null) return "—";
  return new Intl.NumberFormat("fr-CA", {
    style: "currency",
    currency: "CAD",
    maximumFractionDigits: 0
  }).format(n);
}

function fmtDate(d: string | null | undefined): string {
  if (!d) return "—";
  return d.slice(0, 10);
}

/** Ligne « loyer du mois » — champs communs à /loyers/overview (interne)
 *  et /loyers/externes (gestion externe). */
type LoyerMoisLigne = {
  logement_id?: number | null;
  loyer_mensuel: number;
  montant_paye: number | null;
  solde_total?: number;
};

function moisCourant(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

const moisCourantLisible = new Date().toLocaleDateString("fr-CA", {
  month: "long",
  year: "numeric"
});

function StatutBadge({ status }: { status: string }) {
  const map: Record<string, { cls: string; label: string }> = {
    occupe: { cls: "badge-emerald", label: "Occupé" },
    vacant: { cls: "badge-amber", label: "Vacant" },
    reserve: { cls: "badge-sky", label: "Réservé" },
    hors_location: { cls: "badge-neutral", label: "Hors loc." }
  };
  const t = map[status] || { cls: "badge-neutral", label: status };
  return <span className={`badge ${t.cls}`}>{t.label}</span>;
}

/** Gestion EXTERNE (retour Phil 2026-09-09) : pas de bail ni de
 *  relocation dans Kratos — juste un NOM de locataire facultatif sur
 *  l'unité (repère pour le rapport mensuel du gestionnaire) et un
 *  « Départ » qui rend l'unité vacante et efface le nom. */
function LocataireExterneActions({
  logementId,
  nom,
  nomBail,
  statut,
  onDone
}: {
  logementId: number;
  nom: string | null;
  /** Nom porté par un bail résiduel (créé avant la règle), en filet. */
  nomBail: string | null;
  statut: string;
  onDone: () => void;
}) {
  const [valeur, setValeur] = useState(nom ?? nomBail ?? "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    setValeur(nom ?? nomBail ?? "");
  }, [nom, nomBail]);

  async function patch(body: Record<string, unknown>) {
    setBusy(true);
    setMsg(null);
    try {
      const r = await authedFetch(`/api/v1/immobilier/logements/${logementId}`, {
        method: "PATCH",
        body: JSON.stringify(body)
      });
      if (!r.ok) throw new Error((await r.text()).slice(0, 200));
      onDone();
    } catch (e) {
      setMsg((e as Error).message || "Échec.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="badge badge-sky" title="Perception déléguée à la compagnie de gestion">
        Gestion externe
      </span>
      <input
        type="text"
        value={valeur}
        onChange={(e) => setValeur(e.target.value)}
        placeholder="Nom du locataire (facultatif)"
        className="input w-56 py-1 text-xs"
      />
      <button
        type="button"
        disabled={busy || (valeur.trim() || "") === (nom ?? "")}
        onClick={() =>
          void patch({
            locataire_externe_nom: valeur.trim() || null,
            ...(valeur.trim() && statut === "vacant" ? { status: "occupe" } : {})
          })
        }
        className="inline-flex items-center gap-1.5 rounded-lg border border-accent-500/40 bg-accent-500/10 px-2.5 py-1 text-xs font-semibold text-accent-500 transition hover:bg-accent-500/20 disabled:opacity-50"
      >
        Enregistrer le nom
      </button>
      {statut !== "vacant" ? (
        <button
          type="button"
          disabled={busy}
          title="Le locataire part : l'unité devient vacante et le nom s'efface (rien d'autre en gestion externe)"
          onClick={() => {
            if (window.confirm("Marquer ce logement vacant et effacer le nom du locataire ?"))
              void patch({ status: "vacant" });
          }}
          className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs font-semibold text-amber-300 transition hover:bg-amber-500/20 disabled:opacity-50"
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
          Départ
        </button>
      ) : null}
      {msg ? <span className="text-xs text-rose-300">{msg}</span> : null}
    </div>
  );
}

export default function LogementDetailPage({
  params
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const logementId = Number(id);
  const router = useRouter();
  const searchParams = useSearchParams();
  const fromImmeuble = searchParams.get("from") === "immeuble";
  const [dossier, setDossier] = useState<Dossier | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Loyer du MOIS COURANT (lecture seule) : mêmes chiffres que la page
  // Paiements — interne via /loyers/overview, gestion externe via
  // /loyers/externes. La saisie reste sur les surfaces dédiées.
  const [loyerMois, setLoyerMois] = useState<LoyerMoisLigne | null>(null);

  // Édition INLINE des infos (plus de modale) + notes.
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editErr, setEditErr] = useState<string | null>(null);
  const [form, setForm] = useState({
    numero: "",
    nb_pieces_decimal: "",
    nb_chambres: "",
    nb_sdb: "",
    superficie_pi2: "",
    location_en_chambres: false,
    etage: "",
    type: "residentiel",
    status: "vacant",
    loyer_demande: ""
  });
  const [notesDraft, setNotesDraft] = useState("");
  const [notesSaving, setNotesSaving] = useState(false);
  const [notesSaved, setNotesSaved] = useState(false);

  const loadDossier = useCallback(async () => {
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/logements/${logementId}/dossier`
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = (await r.json()) as Dossier;
      setDossier(d);
      setNotesDraft(d.logement.notes || "");
    } catch (e) {
      setError((e as Error).message);
    }
  }, [logementId]);

  useEffect(() => {
    void loadDossier();
  }, [loadDossier]);

  //: MÊME endpoint que la page Baux, filtré sur ce logement.
  const [suiviBaux, setSuiviBaux] = useState<SuiviBailRow[] | null>(null);

  const loadSuiviBaux = useCallback(async () => {
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/suivi-baux?logement_id=${logementId}`
      );
      if (r.ok) setSuiviBaux((await r.json()) as SuiviBailRow[]);
    } catch {
      /* la fiche reste utilisable sans le tableau */
    }
  }, [logementId]);

  useEffect(() => {
    void loadSuiviBaux();
  }, [loadSuiviBaux]);



  useEffect(() => {
    let annule = false;
    void (async () => {
      const mois = moisCourant();
      const [ri, rx] = await Promise.all([
        authedFetch(`/api/v1/immobilier/loyers/overview?mois=${mois}`),
        authedFetch(`/api/v1/immobilier/loyers/externes?mois=${mois}`)
      ]);
      if (annule) return;
      let ligne: LoyerMoisLigne | null = null;
      if (ri.ok) {
        const d = (await ri.json()) as { rows: LoyerMoisLigne[] };
        ligne = d.rows.find((r) => r.logement_id === logementId) || null;
      }
      if (!ligne && rx.ok) {
        const d = (await rx.json()) as { rows: LoyerMoisLigne[] };
        ligne = d.rows.find((r) => r.logement_id === logementId) || null;
      }
      if (!annule) setLoyerMois(ligne);
    })();
    return () => {
      annule = true;
    };
  }, [logementId]);

  function startEdit() {
    const l = dossier?.logement;
    if (!l) return;
    setForm({
      numero: l.numero || "",
      nb_pieces_decimal:
        l.nb_pieces_decimal != null ? String(l.nb_pieces_decimal) : "",
      nb_chambres: l.nb_chambres != null ? String(l.nb_chambres) : "",
      nb_sdb: l.nb_sdb != null ? String(l.nb_sdb) : "",
      superficie_pi2:
        l.superficie_pi2 != null ? String(l.superficie_pi2) : "",
      location_en_chambres: !!l.location_en_chambres,
      etage: l.etage != null ? String(l.etage) : "",
      type: l.type || "residentiel",
      status: l.status || "vacant",
      loyer_demande: l.loyer_demande != null ? String(l.loyer_demande) : ""
    });
    setEditErr(null);
    setEditing(true);
  }

  async function saveEdit() {
    setSaving(true);
    setEditErr(null);
    try {
      const num = (v: string) => (v.trim() === "" ? null : Number(v));
      const r = await authedFetch(
        `/api/v1/immobilier/logements/${logementId}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            numero: form.numero.trim() || undefined,
            nb_pieces_decimal: num(form.nb_pieces_decimal),
            nb_chambres: num(form.nb_chambres),
            nb_sdb: num(form.nb_sdb),
            superficie_pi2: num(form.superficie_pi2),
            location_en_chambres: form.location_en_chambres,
            etage: num(form.etage),
            type: form.type,
            status: form.status,
            loyer_demande: num(form.loyer_demande)
          })
        }
      );
      if (!r.ok) {
        const t = await r.text();
        throw new Error(t.slice(0, 200) || `HTTP ${r.status}`);
      }
      await loadDossier();
      setEditing(false);
    } catch (e) {
      setEditErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function saveNotes() {
    setNotesSaving(true);
    setNotesSaved(false);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/logements/${logementId}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            notes: notesDraft.trim() ? notesDraft : null
          })
        }
      );
      if (r.ok) {
        setNotesSaved(true);
        window.setTimeout(() => setNotesSaved(false), 2500);
      }
    } finally {
      setNotesSaving(false);
    }
  }

  // « Départ » / « Relouer » : ouvre un dossier de relocation dans
  // Locations (depuis le bail actif, ou le logement s'il est vacant).
  const [relocBusy, setRelocBusy] = useState(false);
  const [relocMsg, setRelocMsg] = useState<string | null>(null);

  async function ouvrirRelocation(bailId: number | null) {
    setRelocBusy(true);
    setRelocMsg(null);
    try {
      const r = await authedFetch("/api/v1/immobilier/locations", {
        method: "POST",
        body: JSON.stringify(
          bailId != null
            ? { bail_id: bailId }
            : { logement_id: logementId }
        )
      });
      if (!r.ok) {
        const t = await r.text();
        throw new Error(
          t.includes("déjà en cours")
            ? "Une relocation est déjà en cours pour ce logement."
            : t.slice(0, 200) || `HTTP ${r.status}`
        );
      }
      setRelocMsg("Dossier de relocation créé — suivi dans Locations.");
    } catch (e) {
      setRelocMsg((e as Error).message);
    } finally {
      setRelocBusy(false);
    }
  }

  async function deleteLogement() {
    if (
      !window.confirm(
        "Supprimer ce logement ? Ses baux et son historique seront supprimés."
      )
    )
      return;
    const r = await authedFetch(
      `/api/v1/immobilier/logements/${logementId}`,
      { method: "DELETE" }
    );
    if (r.ok) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      router.push("/immobilier/logements" as any);
    } else {
      setEditErr("Suppression impossible.");
    }
  }

  const lg = dossier?.logement ?? null;
  // Gestion EXTERNE : le loyer SAISI sur le logement est la vérité —
  // un bail résiduel dans Kratos ne pilote plus l'affichage (2026-08-14).
  const externe = !!dossier?.immeuble?.gestion_externe;
  const bailActif = dossier
    ? dossier.baux.find((b) => b.status === "actif") ||
      dossier.baux.find((b) => b.status === "propose") ||
      null
    : null;
  // TOUS les baux, y compris l'ACTUEL (retour Phil 2026-07-31 :
  // « il faudrait qu'il y ait celui présent aussi dedans »).
    const maxLoyer = dossier
    ? Math.max(...dossier.historique_loyer.map((p) => p.loyer_mensuel), 1)
    : 1;

  return (
    <>
      <ImmobilierTopbar
        breadcrumbs={[
          { label: "Gestion immobilière", href: "/immobilier" },
          { label: "Logements", href: "/immobilier/logements" },
          { label: lg ? `Logement ${lg.numero}` : "Logement" }
        ]}
      />
      <div className="p-4 lg:p-6 pb-28">
        {/* Retour CONTEXTUEL (retour Phil 2026-07-10) : arrivé depuis la
            fiche immeuble → on y retourne, onglet Logements ouvert ;
            sinon → liste des logements. */}
        {fromImmeuble && dossier ? (
          <Link
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            href={
              `/immobilier/immeubles/${dossier.immeuble.id}?tab=logements` as any
            }
            className="inline-flex items-center text-xs text-white/50 hover:text-accent-500"
          >
            <ArrowLeft className="mr-1 h-3.5 w-3.5" />
            {dossier.immeuble.name} · Logements
          </Link>
        ) : (
          <Link
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            href={"/immobilier/logements" as any}
            className="inline-flex items-center text-xs text-white/50 hover:text-accent-500"
          >
            <ArrowLeft className="mr-1 h-3.5 w-3.5" /> Logements
          </Link>
        )}

        {error ? (
          <p className="mt-4 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">
            {error}
          </p>
        ) : !dossier || !lg ? (
          <div className="mt-6 flex items-center gap-2 text-xs text-white/50">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Chargement…
          </div>
        ) : (
          <div className="mt-4 space-y-6">
            {/* Header */}
            <header className="flex items-start gap-4">
              <span className="flex h-14 w-14 items-center justify-center rounded-xl bg-accent-500/15 text-accent-500">
                <DoorOpen className="h-6 w-6" />
              </span>
              <div className="min-w-0">
                <h1 className="flex flex-wrap items-center gap-3 text-2xl font-bold text-white">
                  Logement {lg.numero} — {dossier.immeuble.name}
                  <StatutBadge status={lg.status} />
                </h1>
                <p className="mt-1 flex items-center gap-1.5 text-sm text-white/60">
                  <Building2 className="h-3.5 w-3.5 text-white/40" />
                  {dossier.immeuble.address || dossier.immeuble.name}
                </p>
              </div>
              <div className="ml-auto flex shrink-0 items-center gap-2">
                {editing ? (
                  <>
                    <button
                      type="button"
                      onClick={deleteLogement}
                      className="btn-sm inline-flex items-center gap-1.5 rounded-lg border border-rose-400/30 bg-rose-500/10 px-3 py-1.5 text-xs font-semibold text-rose-300 hover:bg-rose-500/20"
                    >
                      <Trash2 className="h-3.5 w-3.5" /> Supprimer
                    </button>
                    <button
                      type="button"
                      onClick={() => setEditing(false)}
                      className="btn-secondary btn-sm"
                    >
                      <X className="h-4 w-4" /> Annuler
                    </button>
                    <button
                      type="button"
                      onClick={saveEdit}
                      disabled={saving}
                      className="btn-accent btn-sm disabled:opacity-60"
                    >
                      {saving ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Check className="h-4 w-4" />
                      )}
                      Enregistrer
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    onClick={startEdit}
                    className="btn-secondary btn-sm"
                    title="Modifier les informations directement dans la page"
                  >
                    <Pencil className="h-4 w-4" />
                    Modifier
                  </button>
                )}
              </div>
            </header>

            {editErr ? (
              <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                {editErr}
              </p>
            ) : null}

            {/* Mini-KPIs — hiérarchie du loyer effectif (retour client
                2026-08-14) : gestion EXTERNE → le loyer SAISI est LA
                vérité ; interne OCCUPÉ → le loyer RÉEL du bail (le
                « demandé » le suit automatiquement, pas de doublon) ;
                vacant → le demandé seul. */}
            <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              {externe ? (
                <MiniKpi
                  label="Loyer mensuel"
                  value={
                    lg.loyer_demande != null ? money(lg.loyer_demande) : "—"
                  }
                  sub="Gestion externe — loyer saisi sur le logement"
                />
              ) : bailActif ? (
                <MiniKpi
                  label="Loyer actuel (bail)"
                  value={money(bailActif.loyer_mensuel)}
                  href="#bail-actif"
                  sub="Voir le bail"
                />
              ) : (
                <MiniKpi
                  label="Loyer demandé"
                  value={
                    lg.loyer_demande != null ? money(lg.loyer_demande) : "—"
                  }
                  sub="Vacant — prix affiché pour la relocation"
                />
              )}
              <MiniKpi
                label="Occupé depuis"
                value={bailActif ? fmtDate(bailActif.date_debut) : "—"}
              />
              <MiniKpi
                label="Rénos & maintenance"
                value={money(
                  dossier.bons_travail.reduce(
                    (s, b) => s + (b.montant || 0),
                    0
                  )
                )}
              />
            </section>

            {/* (a) Infos + (b) Locataire actuel & bail actif */}
            <section className="grid gap-4 lg:grid-cols-2">
              <div className="rounded-2xl border border-brand-800 bg-brand-900 p-5">
                <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-accent-500">
                  Infos
                </h2>
                {editing ? (
                  <div className="grid grid-cols-2 gap-3 text-sm">
                    <EditField label="Numéro">
                      <input
                        value={form.numero}
                        onChange={(e) =>
                          setForm((f) => ({ ...f, numero: e.target.value }))
                        }
                        className={inputCls}
                      />
                    </EditField>
                    <EditField label="Pièces (ex. 3.5 = 3½)">
                      <input
                        inputMode="decimal"
                        value={form.nb_pieces_decimal}
                        onChange={(e) =>
                          setForm((f) => ({
                            ...f,
                            nb_pieces_decimal: e.target.value
                          }))
                        }
                        className={inputCls}
                      />
                    </EditField>
                    <EditField label="Chambres">
                      <input
                        inputMode="numeric"
                        value={form.nb_chambres}
                        onChange={(e) =>
                          setForm((f) => ({
                            ...f,
                            nb_chambres: e.target.value
                          }))
                        }
                        className={inputCls}
                      />
                    </EditField>
                    <EditField label="Salles de bain">
                      <input
                        inputMode="decimal"
                        value={form.nb_sdb}
                        onChange={(e) =>
                          setForm((f) => ({ ...f, nb_sdb: e.target.value }))
                        }
                        className={inputCls}
                      />
                    </EditField>
                    <EditField label="Superficie (pi²)">
                      <input
                        inputMode="decimal"
                        value={form.superficie_pi2}
                        onChange={(e) =>
                          setForm((f) => ({
                            ...f,
                            superficie_pi2: e.target.value
                          }))
                        }
                        className={inputCls}
                      />
                    </EditField>
                    <EditField label={LOUER_INDEFINIMENT_LABEL}>
                      <label
                        title={LOUER_INDEFINIMENT_INFO}
                        className="flex h-9 cursor-pointer items-center gap-2 text-sm text-white/80"
                      >
                        <input
                          type="checkbox"
                          checked={form.location_en_chambres}
                          onChange={(e) =>
                            setForm((f) => ({
                              ...f,
                              location_en_chambres: e.target.checked
                            }))
                          }
                          className="h-4 w-4 accent-accent-500"
                        />
                        Loyer figé, bail au mois
                      </label>
                    </EditField>
                    {form.location_en_chambres ? (
                      <LouerIndefinimentBulle className="col-span-2" />
                    ) : null}
                    <EditField label="Étage">
                      <input
                        inputMode="numeric"
                        value={form.etage}
                        onChange={(e) =>
                          setForm((f) => ({ ...f, etage: e.target.value }))
                        }
                        className={inputCls}
                      />
                    </EditField>
                    <EditField label="Type">
                      <select
                        value={form.type}
                        onChange={(e) =>
                          setForm((f) => ({ ...f, type: e.target.value }))
                        }
                        className={inputCls}
                      >
                        <option value="residentiel">Résidentiel</option>
                        <option value="commercial">Commercial</option>
                        <option value="mixte">Mixte</option>
                        <option value="unifamilial">Unifamilial</option>
                        <option value="autre">Autre</option>
                      </select>
                    </EditField>
                    <EditField label="Statut">
                      <select
                        value={form.status}
                        onChange={(e) =>
                          setForm((f) => ({ ...f, status: e.target.value }))
                        }
                        className={inputCls}
                      >
                        <option value="occupe">Occupé</option>
                        <option value="vacant">Vacant</option>
                        <option value="reserve">Réservé</option>
                        <option value="hors_location">Hors location</option>
                      </select>
                    </EditField>
                    {!externe && bailActif ? (
                      // Logement LOUÉ : le loyer demandé SUIT le bail
                      // automatiquement (retour client 2026-08-14) — le
                      // prix de la prochaine location se décide à la
                      // relocation, pas ici.
                      <EditField label="Loyer ($/mois)">
                        <input
                          value={money(bailActif.loyer_mensuel)}
                          disabled
                          className={`${inputCls} opacity-60`}
                        />
                        <span className="mt-0.5 block text-[10px] font-normal text-white/40">
                          Suit le bail en cours (avis d&apos;augmentation
                          inclus). Le prix de la prochaine location se
                          décide au moment de la relocation.
                        </span>
                      </EditField>
                    ) : (
                      <EditField
                        label={
                          externe
                            ? "Loyer mensuel ($/mois)"
                            : "Loyer demandé ($/mois)"
                        }
                      >
                        <input
                          inputMode="decimal"
                          value={form.loyer_demande}
                          onChange={(e) =>
                            setForm((f) => ({
                              ...f,
                              loyer_demande: e.target.value
                            }))
                          }
                          className={inputCls}
                        />
                        {externe ? (
                          <span className="mt-0.5 block text-[10px] font-normal text-white/40">
                            Gestion externe : ce montant fait foi partout
                            (listes, paiements, cashflow).
                          </span>
                        ) : null}
                      </EditField>
                    )}
                  </div>
                ) : (
                  <dl className="space-y-1.5 text-sm">
                    <Row label="Type" value={lg.type} />
                    <Row
                      label="Pièces"
                      value={
                        lg.location_en_chambres
                          ? "Chambre"
                          : fmtPieces(lg.nb_pieces_decimal)
                      }
                    />
                    <Row
                      label="Chambres"
                      value={
                        lg.nb_chambres != null ? String(lg.nb_chambres) : "—"
                      }
                    />
                    <Row
                      label="Salles de bain"
                      value={lg.nb_sdb != null ? String(lg.nb_sdb) : "—"}
                    />
                    <Row
                      label="Superficie"
                      value={
                        lg.superficie_pi2 != null
                          ? `${lg.superficie_pi2} pi²`
                          : "—"
                      }
                    />
                    <Row
                      label="Étage"
                      value={lg.etage != null ? String(lg.etage) : "—"}
                    />
                    {/* Hiérarchie du loyer effectif (2026-08-14) :
                        externe → loyer SAISI ; interne occupé → loyer
                        RÉEL du bail (le demandé le suit, pas de
                        doublon) ; vacant → demandé seul. */}
                    {externe ? (
                      <Row
                        label="Loyer mensuel (gestion externe)"
                        value={
                          lg.loyer_demande != null
                            ? money(lg.loyer_demande)
                            : "—"
                        }
                      />
                    ) : bailActif ? (
                      <Row
                        label="Loyer actuel (bail)"
                        value={money(bailActif.loyer_mensuel)}
                      />
                    ) : (
                      <Row
                        label="Loyer demandé"
                        value={
                          lg.loyer_demande != null
                            ? money(lg.loyer_demande)
                            : "—"
                        }
                      />
                    )}
                    {lg.location_en_chambres ? (
                      <>
                        <Row
                          label={LOUER_INDEFINIMENT_LABEL}
                          value="Oui — loyer figé, bail au mois"
                        />
                        <LouerIndefinimentBulle className="mt-1" />
                      </>
                    ) : null}
                  </dl>
                )}
              </div>

              {/* id : cible du lien « Voir le bail » du KPI Loyer actuel. */}
              <div
                id="bail-actif"
                className="rounded-2xl border border-brand-800 bg-brand-900 p-5"
              >
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                  <h2 className="text-sm font-semibold uppercase tracking-wider text-accent-500">
                    Locataire actuel &amp; bail actif
                  </h2>
                  {externe ? (
                    <LocataireExterneActions
                      logementId={logementId}
                      nom={lg.locataire_externe_nom ?? null}
                      nomBail={
                        bailActif?.locataire?.full_name ?? null
                      }
                      statut={lg.status}
                      onDone={() => void loadDossier()}
                    />
                  ) : (
                  <div className="flex items-center gap-2">
                    {!bailActif ? (
                      <AssignerBailButton
                        mode="logement"
                        logementId={logementId}
                        logementLabel={`Logement ${lg.numero}`}
                        logementEnChambres={!!lg.location_en_chambres}
                        onDone={() => void loadDossier()}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-accent-500/40 bg-accent-500/10 px-2.5 py-1 text-xs font-semibold text-accent-500 transition hover:bg-accent-500/20"
                      />
                    ) : null}
                    <button
                      type="button"
                      disabled={relocBusy}
                      title={
                        bailActif
                          ? "Le locataire confirme son départ — ouvrir un dossier de relocation (Locations)"
                          : "Ouvrir un dossier de relocation pour ce logement vacant"
                      }
                      onClick={() =>
                        void ouvrirRelocation(bailActif ? bailActif.id : null)
                      }
                      className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs font-semibold text-amber-300 transition hover:bg-amber-500/20 disabled:opacity-50"
                    >
                      {relocBusy ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : null}
                      {bailActif ? "Départ" : "Relouer"}
                    </button>
                  </div>
                  )}
                </div>
                {relocMsg ? (
                  <p className="mb-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                    {relocMsg}{" "}
                    <Link
                      // eslint-disable-next-line @typescript-eslint/no-explicit-any
                      href={"/immobilier/locations" as any}
                      className="underline-offset-2 hover:underline"
                    >
                      Ouvrir Locations →
                    </Link>
                  </p>
                ) : null}
                {bailActif ? (
                  <div className="space-y-2 text-sm">
                    {bailActif.locataire ? (
                      <Link
                        // eslint-disable-next-line @typescript-eslint/no-explicit-any
                        href={
                          `/immobilier/locataires/${bailActif.locataire.id}` as any
                        }
                        className="inline-flex items-center gap-1.5 font-medium text-accent-500 hover:underline"
                      >
                        <User className="h-4 w-4" />
                        {bailActif.locataire.full_name}
                      </Link>
                    ) : (
                      <p className="text-white/50">Locataire inconnu</p>
                    )}
                    <dl className="space-y-1.5">
                      <Row
                        label="Loyer"
                        value={
                          // Bail TAL « Ou le ___ » : muet quand c'est le
                          // 1er (l'immense majorité des baux).
                          `${money(bailActif.loyer_mensuel)}/mois` +
                          (echeanceLabel(bailActif.jour_echeance)
                            ? ` · ${echeanceLabel(bailActif.jour_echeance)}`
                            : "")
                        }
                      />
                      <Row
                        label="Période"
                        value={
                          bailActif.au_mois
                            ? `${fmtDate(bailActif.date_debut)} → au mois`
                            : `${fmtDate(bailActif.date_debut)} → ${fmtDate(bailActif.date_fin)}`
                        }
                      />
                    </dl>
                    {/* Même empilement Loyer / Reçu / Solde que la page
                        Paiements et les fiches immeuble/locataire —
                        « partout dans la colonne loyer » (Phil
                        2026-08-13). Vaut aussi en gestion externe, où le
                        suivi se fait par logement. */}
                    {loyerMois ? (
                      <div className="rounded-xl border border-brand-800 bg-brand-950/40 p-3">
                        <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-white/60">
                          Loyer du mois — {moisCourantLisible}
                        </p>
                        <CelluleLoyer
                          loyer={loyerMois.loyer_mensuel}
                          recu={loyerMois.montant_paye}
                          solde={loyerMois.solde_total}
                          fmt={money}
                        />
                      </div>
                    ) : null}
                    <div className="flex flex-wrap items-center gap-2 pt-1">
                      <span
                        className={`badge ${BAIL_STATUS_BADGE[bailActif.status] || "badge-neutral"}`}
                      >
                        {BAIL_STATUS_LABEL[bailActif.status] ??
                          bailActif.status}
                      </span>
                      {bailActif.relocation_statut ? (
                        // M1 (audit 2026-08-13) : pastille kanban
                        // complète — le bail SORTANT montre aussi son
                        // cycle de départ (avis reçu, visites…).
                        <RelocationStatutPastille
                          statut={bailActif.relocation_statut}
                          dossierId={bailActif.relocation_dossier_id}
                        />
                      ) : null}
                      <AuMoisToggle
                        bailId={bailActif.id}
                        auMois={!!bailActif.au_mois}
                        onChanged={() => void loadDossier()}
                      />
                      {/* Bail TAL « Ou le ___ » — modifiable ici comme
                          sur la page Baux (miroir bidirectionnel). */}
                      <JourEcheanceInline
                        compact
                        bailId={bailActif.id}
                        jour={bailActif.jour_echeance}
                        onChanged={() => void loadDossier()}
                      />
                      <ResilierBailButton
                        bailId={bailActif.id}
                        locataireNom={bailActif.locataire?.full_name}
                        immeubleName={dossier?.immeuble.name}
                        logementNumero={lg.numero}
                        onMessage={setRelocMsg}
                        onChanged={() => void loadDossier()}
                      />
                      <BailDocActions
                        bailId={bailActif.id}
                        hasDoc={bailActif.document_id != null}
                        signedAt={bailActif.signed_at}
                        onChanged={() => void loadDossier()}
                      />
                    </div>
                  </div>
                ) : (
                  <p className="text-sm text-white/50">
                    Aucun bail actif — logement libre.
                  </p>
                )}
              </div>
            </section>

            {/* (c) Historique des locataires */}
            <section className="rounded-2xl border border-brand-800 bg-brand-900 p-5">
              <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-accent-500">
                Historique des locataires
              </h2>
              {/* MIROIR : exactement le tableau de la page Baux,
                  filtré sur ce logement. Une seule implémentation —
                  deux versions de la même vue divergent toujours
                  (retour Phil 2026-08-19). */}
              {suiviBaux === null ? (
                <p className="flex items-center gap-2 text-xs text-white/50">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Chargement…
                </p>
              ) : suiviBaux.length === 0 ? (
                <p className="text-sm text-white/50">
                  Aucun bail pour ce logement.
                </p>
              ) : (
                <TableauSuiviBaux
                  rows={suiviBaux}
                  onChanged={() => {
                    void loadSuiviBaux();
                    void loadDossier();
                  }}
                />
              )}
            </section>

            {/* Documents — tout ce qui a été généré pour les baux de ce
                logement (avis TAL, lettres…), retour Phil 2026-07-20. */}
            <DocumentsSection
              logementId={logementId}
              bails={(bailActif ? [bailActif] : []).map((b) => ({
                id: b.id,
                label: b.locataire?.full_name || "Bail actif"
              }))}
            />

            {/* (d) Fluctuation du loyer */}
            <section className="rounded-2xl border border-brand-800 bg-brand-900 p-5">
              <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-accent-500">
                <TrendingUp className="h-4 w-4" />
                Fluctuation du loyer
              </h2>
              {dossier.historique_loyer.length === 0 ? (
                <p className="text-sm text-white/50">
                  Aucun bail — pas encore d&apos;historique de loyer.
                </p>
              ) : (
                <ul className="space-y-2">
                  {dossier.historique_loyer.map((p, i) => {
                    const prev =
                      i > 0 ? dossier.historique_loyer[i - 1] : null;
                    const delta =
                      prev && prev.loyer_mensuel > 0
                        ? ((p.loyer_mensuel - prev.loyer_mensuel) /
                            prev.loyer_mensuel) *
                          100
                        : null;
                    return (
                      <li
                        key={`${p.date_debut}-${i}`}
                        className="flex items-center gap-3 text-sm"
                      >
                        <span className="w-24 shrink-0 font-mono text-xs text-white/50">
                          {fmtDate(p.date_debut)}
                        </span>
                        <span className="h-2 flex-1 overflow-hidden rounded-full bg-brand-950">
                          <span
                            className="block h-full rounded-full bg-accent-500/70"
                            style={{
                              width: `${Math.max(
                                (p.loyer_mensuel / maxLoyer) * 100,
                                2
                              )}%`
                            }}
                          />
                        </span>
                        <span className="w-20 shrink-0 text-right font-mono text-white/80">
                          {money(p.loyer_mensuel)}
                        </span>
                        <span className="w-16 shrink-0 text-right font-mono text-xs">
                          {delta == null ? (
                            <span className="text-white/30">—</span>
                          ) : delta > 0 ? (
                            <span className="text-emerald-300">
                              +{delta.toFixed(1)} %
                            </span>
                          ) : delta < 0 ? (
                            <span className="text-rose-300">
                              {delta.toFixed(1)} %
                            </span>
                          ) : (
                            <span className="text-white/40">0 %</span>
                          )}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>

            {/* (e) Rénos & maintenance */}
            <section className="rounded-2xl border border-brand-800 bg-brand-900 p-5">
              <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-accent-500">
                <Wrench className="h-4 w-4" />
                Rénos &amp; maintenance
              </h2>
              {dossier.bons_travail.length === 0 ? (
                <p className="text-sm text-white/50">
                  Aucun bon de travail rattaché à ce logement.
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[560px] text-left text-sm">
                    <thead className="text-[10px] uppercase tracking-wider text-white/45">
                      <tr>
                        <th className="py-2 pr-3">Référence</th>
                        <th className="py-2 pr-3">Titre</th>
                        <th className="py-2 pr-3 text-right">Statut</th>
                        <th className="py-2 pr-3 text-right">Coût</th>
                        <th className="py-2 text-right">Date</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-brand-800/70">
                      {dossier.bons_travail.map((b) => (
                        <tr key={b.id}>
                          <td className="py-2.5 pr-3 font-mono text-xs text-white/70">
                            {b.reference}
                          </td>
                          <td className="py-2.5 pr-3 font-medium text-white">
                            {b.title}
                          </td>
                          <td className="py-2.5 pr-3 text-right">
                            <span
                              className={`badge ${BON_STATUS_BADGE[b.status] || "badge-neutral"}`}
                            >
                              {BON_STATUS_LABEL[b.status] ?? b.status}
                            </span>
                          </td>
                          <td className="py-2.5 pr-3 text-right text-white/80">
                            {money(b.montant)}
                          </td>
                          <td className="py-2.5 text-right text-xs text-white/60">
                            {fmtDate(b.created_at)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>


            {/* (g) Notes */}
            <section className="rounded-2xl border border-brand-800 bg-brand-900 p-5">
              <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-accent-500">
                <StickyNote className="h-4 w-4" />
                Notes
              </h2>
              <textarea
                rows={4}
                value={notesDraft}
                onChange={(e) => setNotesDraft(e.target.value)}
                placeholder="Particularités du logement, travaux à prévoir, clés/serrures, électros inclus…"
                className="block w-full rounded-md border border-brand-800 bg-brand-950 px-3 py-2 text-sm text-white outline-none focus:border-accent-500"
              />
              <div className="mt-2 flex items-center gap-2">
                <button
                  type="button"
                  onClick={saveNotes}
                  disabled={notesSaving}
                  className="btn-secondary btn-sm disabled:opacity-60"
                >
                  {notesSaving ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Check className="h-3.5 w-3.5" />
                  )}
                  Enregistrer les notes
                </button>
                {notesSaved ? (
                  <span className="text-xs text-emerald-300">
                    Notes enregistrées.
                  </span>
                ) : null}
              </div>
            </section>
          </div>
        )}
      </div>
    </>
  );
}

const inputCls =
  "mt-0.5 block w-full rounded-md border border-brand-800 bg-brand-950 px-2 py-1.5 text-xs text-white outline-none focus:border-accent-500";

function EditField({
  label,
  children
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="text-[11px] font-semibold text-white/60">
      {label}
      {children}
    </label>
  );
}

function MiniKpi({
  label,
  value,
  sub,
  href
}: {
  label: string;
  value: string;
  /** Mention discrète sous la valeur (ex. « prix de la prochaine location »). */
  sub?: string;
  /** Ancre optionnelle (ex. #bail-actif — « Loyer actuel (bail) »). */
  href?: string;
}) {
  const inner = (
    <>
      <div className="text-[10px] font-semibold uppercase tracking-wider text-white/45">
        {label}
      </div>
      <div className="mt-0.5 truncate text-lg font-bold text-white">
        {value}
      </div>
      {sub ? (
        <div className="mt-0.5 truncate text-[10px] text-white/40">{sub}</div>
      ) : null}
    </>
  );
  if (href) {
    return (
      <a
        href={href}
        className="block rounded-2xl border border-brand-800 bg-brand-900 p-3.5 transition hover:border-accent-500/40"
      >
        {inner}
      </a>
    );
  }
  return (
    <div className="rounded-2xl border border-brand-800 bg-brand-900 p-3.5">
      {inner}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <dt className="text-white/50">{label}</dt>
      <dd className="text-right font-medium text-white">{value}</dd>
    </div>
  );
}
