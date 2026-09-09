"use client";

/**
 * « Assigner un locataire à une unité » — retour Phil 2026-07-27 :
 * le lien locataire ↔ logement passe par le BAIL, mais rien ne le
 * disait à l'écran. Ce bouton + modal rend le geste explicite depuis
 * les deux bouts :
 *   - fiche LOGEMENT  → choisir (ou créer) le locataire ;
 *   - fiche LOCATAIRE → choisir l'immeuble puis le logement (vacants
 *     mis en avant).
 * Dans les deux cas on saisit loyer + dates (fin par défaut = prochain
 * 30 juin, standard québécois) + dépôt optionnel, et ça CRÉE le bail.
 * Statut au choix (aligné sur la page Baux) : « proposé » par défaut
 * (suivi via le kanban Locations) ou « déjà en vigueur » (actif — le
 * logement passe « occupé » automatiquement côté serveur).
 */

import { useEffect, useMemo, useState } from "react";
import { Loader2, Plus, Search, UserPlus, X } from "lucide-react";

import { authedFetch } from "@/lib/auth";
import {
  JOUR_ECHEANCE_DEFAUT,
  JourEcheanceField,
  LOUER_INDEFINIMENT_INFO,
  LouerIndefinimentBulle
} from "@/components/immobilier/fin-bail";

type LocataireItem = {
  id: number;
  full_name: string;
  email?: string | null;
  phone?: string | null;
  immeuble_name?: string | null;
  logement_numero?: string | null;
};

type ImmeubleItem = { id: number; name: string };

type LogementItem = {
  id: number;
  numero?: string | null;
  status: string;
  location_en_chambres?: boolean | null;
};

/** Prochain 30 juin « utile » : au moins ~3 mois de bail. */
function finParDefaut(debut: string): string {
  const d = debut ? new Date(debut + "T00:00:00") : new Date();
  let annee = d.getFullYear();
  if (d.getMonth() + 1 >= 4) annee += 1; // après mars → 30 juin suivant
  return `${annee}-06-30`;
}

export function AssignerBailButton({
  mode,
  logementId,
  logementLabel,
  logementEnChambres,
  locataireId,
  locataireNom,
  onDone,
  className
}: {
  mode: "logement" | "locataire";
  logementId?: number;
  logementLabel?: string;
  /** Logement « Louer indéfiniment (chambre) » → bail créé AU MOIS. */
  logementEnChambres?: boolean;
  locataireId?: number;
  locataireNom?: string;
  onDone: () => void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className={className || "btn-outline-accent btn-sm"}
        title={
          mode === "logement"
            ? "Créer le bail qui relie un locataire à ce logement"
            : "Créer le bail qui relie ce locataire à un logement"
        }
        onClick={() => setOpen(true)}
      >
        <UserPlus className="h-3.5 w-3.5" />
        {mode === "logement" ? "Assigner un locataire" : "Assigner à un logement"}
      </button>
      {open ? (
        <AssignerBailModal
          mode={mode}
          logementId={logementId}
          logementLabel={logementLabel}
          logementEnChambres={logementEnChambres}
          locataireId={locataireId}
          locataireNom={locataireNom}
          onClose={() => setOpen(false)}
          onDone={() => {
            setOpen(false);
            onDone();
          }}
        />
      ) : null}
    </>
  );
}

function AssignerBailModal({
  mode,
  logementId,
  logementLabel,
  logementEnChambres,
  locataireId,
  locataireNom,
  onClose,
  onDone
}: {
  mode: "logement" | "locataire";
  logementId?: number;
  logementLabel?: string;
  logementEnChambres?: boolean;
  locataireId?: number;
  locataireNom?: string;
  onClose: () => void;
  onDone: () => void;
}) {
  // Côté locataire (mode logement)
  const [locataires, setLocataires] = useState<LocataireItem[] | null>(null);
  const [rech, setRech] = useState("");
  const [locChoisi, setLocChoisi] = useState<LocataireItem | null>(null);
  const [creerNouveau, setCreerNouveau] = useState(false);
  const [nvNom, setNvNom] = useState("");
  const [nvEmail, setNvEmail] = useState("");
  const [nvTel, setNvTel] = useState("");

  // Côté logement (mode locataire)
  const [immeubles, setImmeubles] = useState<ImmeubleItem[] | null>(null);
  const [immId, setImmId] = useState("");
  const [logements, setLogements] = useState<LogementItem[] | null>(null);
  const [logId, setLogId] = useState("");

  // Bail
  const [loyer, setLoyer] = useState("");
  const [debut, setDebut] = useState(() =>
    new Date().toISOString().slice(0, 10)
  );
  const [fin, setFin] = useState(() => finParDefaut(""));
  const [depot, setDepot] = useState("");
  const [depotRecuLe, setDepotRecuLe] = useState("");
  const [depotDetenteur, setDepotDetenteur] = useState("");
  // Bail TAL « Ou le ___ » : 1er du mois pour l'immense majorité.
  const [jourEcheance, setJourEcheance] = useState(JOUR_ECHEANCE_DEFAUT);
  // Statut de création UNIFIÉ (même défaut que la page Baux) : proposé
  // → suivi via le kanban Locations ; actif → bail déjà signé/en vigueur.
  const [statut, setStatut] = useState<"propose" | "actif">("propose");

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (mode === "logement") {
      void authedFetch("/api/v1/immobilier/locataires").then(async (r) => {
        if (r.ok) setLocataires(await r.json());
      });
    } else {
      void authedFetch("/api/v1/immobilier/immeubles").then(async (r) => {
        if (r.ok) setImmeubles(await r.json());
      });
    }
  }, [mode]);

  useEffect(() => {
    if (mode !== "locataire" || !immId) {
      setLogements(null);
      setLogId("");
      return;
    }
    setLogements(null);
    void authedFetch(
      `/api/v1/immobilier/immeubles/${immId}/logements`
    ).then(async (r) => {
      if (r.ok) {
        const rows = (await r.json()) as LogementItem[];
        // Vacants d'abord — c'est presque toujours eux qu'on assigne.
        rows.sort((a, b) => {
          const va = a.status === "vacant" ? 0 : 1;
          const vb = b.status === "vacant" ? 0 : 1;
          return va - vb || String(a.numero).localeCompare(String(b.numero));
        });
        setLogements(rows);
      }
    });
  }, [mode, immId]);

  // « Louer indéfiniment (chambre) » : c'est le LOGEMENT qui décide, pas
  // une case à cocher du formulaire — le bail naît AU MOIS (le serveur le
  // réimpose de toute façon). Retour Phil 2026-08-13.
  const logementChoisi = useMemo(
    () => (logements || []).find((l) => String(l.id) === logId) || null,
    [logements, logId]
  );
  const indefini =
    mode === "logement"
      ? Boolean(logementEnChambres)
      : Boolean(logementChoisi?.location_en_chambres);

  const resultats = useMemo(() => {
    const q = rech.trim().toLowerCase();
    if (!q || !locataires) return [];
    return locataires
      .filter((l) => l.full_name.toLowerCase().includes(q))
      .slice(0, 8);
  }, [rech, locataires]);

  const assigner = async () => {
    setErr(null);
    const montant = parseFloat(loyer.replace(",", "."));
    if (isNaN(montant) || montant <= 0) {
      setErr("Indique le loyer mensuel.");
      return;
    }
    if (!debut || !fin) {
      setErr("Indique le début et la fin du bail.");
      return;
    }
    if (fin <= debut) {
      setErr("La fin du bail doit être après le début.");
      return;
    }
    let cibleLocataire = locataireId ?? locChoisi?.id ?? null;
    const cibleLogement =
      logementId ?? (logId ? parseInt(logId, 10) : null);
    if (mode === "logement" && creerNouveau && !nvNom.trim()) {
      setErr("Indique le nom du nouveau locataire.");
      return;
    }
    if (!creerNouveau && cibleLocataire == null) {
      setErr("Choisis le locataire (ou crée-le).");
      return;
    }
    if (cibleLogement == null) {
      setErr("Choisis l'immeuble puis le logement.");
      return;
    }
    setBusy(true);
    try {
      if (mode === "logement" && creerNouveau) {
        const r = await authedFetch("/api/v1/immobilier/locataires", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            full_name: nvNom.trim(),
            email: nvEmail.trim() || null,
            phone: nvTel.trim() || null
          })
        });
        if (!r.ok) throw new Error("Création du locataire impossible.");
        cibleLocataire = ((await r.json()) as { id: number }).id;
      }
      const rb = await authedFetch("/api/v1/immobilier/baux", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          logement_id: cibleLogement,
          locataire_id: cibleLocataire,
          date_debut: debut,
          date_fin: fin,
          loyer_mensuel: montant,
          depot_garantie: depot.trim()
            ? parseFloat(depot.replace(",", "."))
            : null,
          depot_recu_le: depotRecuLe || null,
          depot_detenteur: depotDetenteur.trim() || null,
          jour_echeance: jourEcheance,
          status: statut,
          au_mois: indefini ? true : null
        })
      });
      if (!rb.ok) {
        const d = await rb.json().catch(() => null);
        throw new Error(
          (d && (d.detail || d.message)) || "Création du bail impossible."
        );
      }
      onDone();
    } catch (e: any) {
      setErr(e?.message || "Assignation impossible.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="max-h-[85vh] w-full max-w-md overflow-y-auto rounded-2xl border border-brand-800 bg-brand-900 p-5 shadow-card"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <h3 className="text-base font-bold text-white">
              {mode === "logement"
                ? `Assigner un locataire${logementLabel ? ` — ${logementLabel}` : ""}`
                : `Assigner ${locataireNom || "ce locataire"} à un logement`}
            </h3>
            <p className="mt-0.5 text-xs text-white/50">
              Ça crée le bail : c&apos;est lui qui relie le locataire à
              l&apos;unité (loyers, renouvellements, documents).
            </p>
          </div>
          <button
            className="rounded-lg p-1.5 text-white/40 hover:text-white"
            onClick={onClose}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {err ? (
          <p className="mb-3 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
            {err}
          </p>
        ) : null}

        {mode === "logement" ? (
          <div className="space-y-2">
            <label className="text-xs font-medium text-white/60">
              Locataire
            </label>
            {locChoisi ? (
              <div className="flex items-center justify-between rounded-lg border border-accent-500/50 bg-accent-500/5 px-3 py-2">
                <span className="text-sm text-white">
                  {locChoisi.full_name}
                  {locChoisi.immeuble_name ? (
                    <span className="ml-2 text-xs text-amber-300">
                      déjà à bail : {locChoisi.immeuble_name}
                      {locChoisi.logement_numero
                        ? ` · ${locChoisi.logement_numero}`
                        : ""}
                    </span>
                  ) : null}
                </span>
                <button
                  className="text-white/50 hover:text-rose-400"
                  onClick={() => setLocChoisi(null)}
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            ) : creerNouveau ? (
              <div className="space-y-2 rounded-lg border border-brand-800 bg-brand-950/60 p-3">
                <input
                  value={nvNom}
                  onChange={(e) => setNvNom(e.target.value)}
                  placeholder="Nom complet *"
                  className="input w-full"
                />
                <input
                  value={nvEmail}
                  onChange={(e) => setNvEmail(e.target.value)}
                  placeholder="Courriel"
                  className="input w-full"
                />
                <input
                  value={nvTel}
                  onChange={(e) => setNvTel(e.target.value)}
                  placeholder="Téléphone"
                  className="input w-full"
                />
                <button
                  className="text-xs text-white/50 underline hover:text-white"
                  onClick={() => setCreerNouveau(false)}
                >
                  ← choisir un locataire existant
                </button>
              </div>
            ) : (
              <>
                <div className="relative">
                  <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-white/40" />
                  <input
                    value={rech}
                    onChange={(e) => setRech(e.target.value)}
                    placeholder={
                      locataires === null
                        ? "Chargement…"
                        : "Rechercher un locataire…"
                    }
                    className="input w-full pl-8"
                  />
                  {resultats.length > 0 && (
                    <div className="absolute z-10 mt-1 w-full rounded-lg border border-brand-800 bg-brand-950 py-1 shadow-card">
                      {resultats.map((l) => (
                        <button
                          key={l.id}
                          className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-white hover:bg-brand-900"
                          onClick={() => {
                            setLocChoisi(l);
                            setRech("");
                          }}
                        >
                          <span className="truncate">{l.full_name}</span>
                          {l.immeuble_name ? (
                            <span className="ml-auto shrink-0 text-xs text-white/45">
                              {l.immeuble_name}
                            </span>
                          ) : null}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                <button
                  className="inline-flex items-center gap-1 text-xs text-accent-500 hover:underline"
                  onClick={() => setCreerNouveau(true)}
                >
                  <Plus className="h-3 w-3" /> Nouveau locataire
                </button>
              </>
            )}
          </div>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2">
            <div>
              <label className="text-xs font-medium text-white/60">
                Immeuble
              </label>
              <select
                value={immId}
                onChange={(e) => setImmId(e.target.value)}
                className="input mt-1 w-full"
              >
                <option value="">
                  {immeubles === null ? "Chargement…" : "Choisir…"}
                </option>
                {(immeubles || []).map((i) => (
                  <option key={i.id} value={String(i.id)}>
                    {i.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-xs font-medium text-white/60">
                Logement
              </label>
              <select
                value={logId}
                onChange={(e) => setLogId(e.target.value)}
                disabled={!immId}
                className="input mt-1 w-full disabled:opacity-50"
              >
                <option value="">
                  {!immId
                    ? "Choisis l'immeuble d'abord"
                    : logements === null
                      ? "Chargement…"
                      : "Choisir…"}
                </option>
                {(logements || []).map((l) => (
                  <option key={l.id} value={String(l.id)}>
                    {l.numero || `Logement ${l.id}`}
                    {l.status === "vacant" ? " — vacant" : ` — ${l.status}`}
                  </option>
                ))}
              </select>
            </div>
          </div>
        )}

        <div className="mt-4 grid gap-2 sm:grid-cols-2">
          <div>
            <label className="text-xs font-medium text-white/60">
              Loyer mensuel ($) *
            </label>
            <input
              inputMode="decimal"
              value={loyer}
              onChange={(e) => setLoyer(e.target.value)}
              placeholder="ex. 1250"
              className="input mt-1 w-full"
            />
          </div>
          <div>
            <label className="text-xs font-medium text-white/60">
              Dépôt de garantie ($)
            </label>
            <input
              inputMode="decimal"
              value={depot}
              onChange={(e) => setDepot(e.target.value)}
              placeholder="optionnel"
              className="input mt-1 w-full"
            />
          </div>
          <div>
            <label className="text-xs font-medium text-white/60">
              Dépôt reçu le
            </label>
            <input
              type="date"
              value={depotRecuLe}
              onChange={(e) => setDepotRecuLe(e.target.value)}
              className="input mt-1 w-full"
            />
          </div>
          <div>
            <label className="text-xs font-medium text-white/60">
              Dépôt détenu par
            </label>
            <input
              value={depotDetenteur}
              onChange={(e) => setDepotDetenteur(e.target.value)}
              placeholder="ex. compte en fidéicommis"
              className="input mt-1 w-full"
            />
          </div>
          <div>
            <label className="text-xs font-medium text-white/60">
              Début du bail *
            </label>
            <input
              type="date"
              value={debut}
              onChange={(e) => {
                setDebut(e.target.value);
                setFin(finParDefaut(e.target.value));
              }}
              className="input mt-1 w-full"
            />
          </div>
          <div>
            <label className="text-xs font-medium text-white/60">
              Fin du bail *
            </label>
            <input
              type="date"
              value={fin}
              onChange={(e) => setFin(e.target.value)}
              className="input mt-1 w-full"
            />
            {indefini ? (
              <p className="mt-1 text-[10px] text-sky-300/80">
                Formalité : le bail se reconduit tout seul au même loyer,
                cette date ne déclenche rien.
              </p>
            ) : null}
          </div>
          <div>
            <JourEcheanceField
              value={jourEcheance}
              onChange={setJourEcheance}
              labelClassName="text-xs font-medium text-white/60"
              selectClassName="input mt-1 w-full"
            />
          </div>
        </div>

        <div className="mt-3 grid gap-1.5">
          <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-brand-800 bg-brand-950/60 px-3 py-2">
            <input
              type="radio"
              checked={statut === "propose"}
              onChange={() => setStatut("propose")}
              className="mt-0.5"
            />
            <span className="text-xs text-white/70">
              <span className="font-semibold text-white">
                À faire suivre via Locations (proposé)
              </span>{" "}
              — la carte apparaît au kanban (« Bail à envoyer ») ;
              importe le PDF signé pour rendre le bail actif.
            </span>
          </label>
          <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-brand-800 bg-brand-950/60 px-3 py-2">
            <input
              type="radio"
              checked={statut === "actif"}
              onChange={() => setStatut("actif")}
              className="mt-0.5"
            />
            <span className="text-xs text-white/70">
              <span className="font-semibold text-white">
                Ce bail est déjà en vigueur (signé)
              </span>{" "}
              — créé directement ACTIF, sans passer par le kanban
              Locations (le logement passe « occupé »).
            </span>
          </label>
        </div>

        {indefini ? (
          <LouerIndefinimentBulle className="mt-3">
            <strong className="font-semibold text-white">
              Ce logement est loué indéfiniment (chambre)
            </strong>{" "}
            : le bail sera créé <strong>au mois</strong> — loyer figé,
            reconduction automatique, aucun renouvellement.{" "}
            {LOUER_INDEFINIMENT_INFO}
          </LouerIndefinimentBulle>
        ) : null}

        <button
          className="btn-accent btn-sm mt-4 w-full justify-center disabled:opacity-50"
          disabled={busy}
          onClick={() => void assigner()}
        >
          {busy ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <UserPlus className="h-4 w-4" />
          )}
          Créer le bail et assigner
        </button>
      </div>
    </div>
  );
}
