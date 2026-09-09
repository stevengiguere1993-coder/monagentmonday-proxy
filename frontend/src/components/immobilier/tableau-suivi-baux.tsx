"use client";

/**
 * Tableau de suivi des baux — UNE seule implémentation.
 *
 * Retour Phil (2026-08-19) : « la section des baux d'une fiche doit être
 * exactement pareille que dans la page Baux, mais juste pour ce
 * locataire-là. Ça va être la même chose pour toutes les sections de la
 * fiche du locataire, et dans la fiche d'un logement. »
 *
 * D'où ce composant : la page Baux, la fiche locataire et la fiche
 * logement l'appellent avec des lignes venant du MÊME endpoint
 * (`/immobilier/suivi-baux`, filtrable par `locataire_id` /
 * `logement_id`). Deux implémentations de la même vue divergent
 * toujours — et c'est celle qu'on regarde le moins qui finit par mentir.
 *
 * Il porte aussi les trois modales du cycle de vie (fin de bail,
 * annulation de départ, création) : les séparer du tableau, c'est
 * garantir qu'une fiche ait le tableau sans les actions.
 */

import { useState } from "react";
import { FileDown, Plus, Trash2 } from "lucide-react";

import { Link } from "@/i18n/navigation";
import { TransfertUniteButton } from "@/components/immobilier/transfert-unite";
import { authedFetch } from "@/lib/auth";
import {
  AnnulerDepartModal,
  CreerBailModal,
  FinBailModal,
  JourEcheanceInline,
  RelocationStatutPastille,
  ResiliationSuivi,
  type SuiviBailRow
} from "@/components/immobilier/fin-bail";
import { BailDocActions } from "@/components/immobilier/tal-avis";
import { RENOUVELLEMENT_BADGES } from "@/components/immobilier/paiements-actions";

type Row = SuiviBailRow;

function money(n: number | null | undefined): string {
  if (n == null) return "—";
  return new Intl.NumberFormat("fr-CA", {
    style: "currency",
    currency: "CAD",
    maximumFractionDigits: 0
  }).format(n);
}

export function TableauSuiviBaux({
  rows,
  onChanged,
  onFlash
}: {
  /** Lignes DÉJÀ filtrées par l'appelant (page complète ou fiche). */
  rows: Row[];
  onChanged: () => void;
  /** Message de confirmation à afficher par l'appelant. */
  onFlash?: (msg: string) => void;
}) {
  const [finBailFor, setFinBailFor] = useState<Row | null>(null);
  const [annulerDepartFor, setAnnulerDepartFor] = useState<Row | null>(null);
  const [creerFor, setCreerFor] = useState<Row | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const filtres = rows;
  const load = onChanged;
  const setFlash = (msg: string) => onFlash?.(msg);

  //: Ouvre un document conservé (avis de renouvellement) dans un onglet.
  async function ouvrirDoc(docId: number) {
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/documents/${docId}/pdf`
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const url = URL.createObjectURL(await r.blob());
      window.open(url, "_blank");
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (e) {
      setErr(`Ouverture échouée : ${(e as Error).message}`);
    }
  }

  async function supprimerBail(r: Row) {
    if (!r.bail_id) return;
    if (
      !window.confirm(
        `⚠️ Supprimer le bail de ${r.locataire_nom || "ce locataire"} (${r.immeuble_name} · ${r.logement_numero}) ?\n\nSes paiements et documents liés seront affectés — pour une fin de bail normale, utilise plutôt « Mettre fin au bail ».`
      )
    )
      return;
    try {
      const res = await authedFetch(`/api/v1/immobilier/baux/${r.bail_id}`, {
        method: "DELETE"
      });
      if (!res.ok && res.status !== 204) {
        const t = await res.text();
        throw new Error(t.slice(0, 200) || `HTTP ${res.status}`);
      }
      setFlash("Bail supprimé.");
      await load();
    } catch (e) {
      setErr(`Suppression : ${(e as Error).message}`);
    }
  }

  return (
    <>
      {err ? (
        <p className="mb-2 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {err}
        </p>
      ) : null}
      <div className="overflow-x-auto rounded-2xl border border-brand-800 bg-brand-900">
        <table className="w-full min-w-[1140px] text-left text-sm">
          <thead className="border-b border-brand-800 bg-brand-950 text-[10px] uppercase tracking-wider text-white/50">
            <tr>
              <th className="px-4 py-2.5">Immeuble · logt</th>
              <th className="px-4 py-2.5">Locataire</th>
              <th className="px-4 py-2.5">Période</th>
              <th className="px-4 py-2.5 text-right">Loyer/m</th>
              <th className="px-4 py-2.5">Suivi</th>
              <th className="px-4 py-2.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-brand-800">
            {filtres.map((r) => {
              const actifAuDossier =
                r.bail_id != null && r.document_id != null;
              return (
                <tr
                  key={r.logement_id}
                  className={
                    r.resiliation_en_cours
                      ? "bg-rose-500/10 hover:bg-rose-500/15"
                      : actifAuDossier
                        ? "bg-emerald-500/10 hover:bg-emerald-500/15"
                        : r.bail_id != null
                          ? "bg-amber-500/5 hover:bg-amber-500/10"
                          : "hover:bg-brand-950/50"
                  }
                >
                  <td className="px-4 py-2.5">
                    <Link
                      // eslint-disable-next-line @typescript-eslint/no-explicit-any
                      href={`/immobilier/immeubles/${r.immeuble_id}` as any}
                      className="block font-bold text-white hover:text-accent-500"
                    >
                      {r.immeuble_name}
                    </Link>
                    <Link
                      // eslint-disable-next-line @typescript-eslint/no-explicit-any
                      href={`/immobilier/logements/${r.logement_id}` as any}
                      className="text-[11px] font-mono text-accent-500 hover:underline"
                    >
                      {r.logement_numero || `#${r.logement_id}`}
                    </Link>
                  </td>
                  <td className="px-4 py-2.5">
                    {r.locataire_id != null ? (
                      <Link
                        // eslint-disable-next-line @typescript-eslint/no-explicit-any
                        href={
                          `/immobilier/locataires/${r.locataire_id}` as any
                        }
                        className="text-accent-500 hover:underline"
                      >
                        {r.locataire_nom || "—"}
                      </Link>
                    ) : (
                      <span className="text-white/40">
                        {r.bail_id != null ? "—" : "Aucun bail"}
                      </span>
                    )}
                    {r.prochain_locataire_nom ? (
                      <div className="mt-0.5 text-[10px] text-orange-300/90">
                        Prochain : {r.prochain_locataire_nom}
                        {r.prochain_loyer != null
                          ? ` · ${money(r.prochain_loyer)}`
                          : ""}
                        {r.prochain_date_debut
                          ? ` dès le ${r.prochain_date_debut}`
                          : ""}
                      </div>
                    ) : null}
                  </td>
                  <td className="px-4 py-2.5 text-xs text-white/60">
                    {r.bail_id != null
                      ? r.au_mois
                        ? `${r.date_debut} → au mois`
                        : `${r.date_debut} → ${r.date_fin}`
                      : "—"}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-white/80">
                    {money(r.loyer_mensuel)}
                    {/* Bail TAL « Ou le ___ » : discret quand c'est le
                        1er, cliquable pour modifier. */}
                    <JourEcheanceInline
                      bailId={r.bail_id}
                      jour={r.jour_echeance}
                      onChanged={load}
                    />
                  </td>
                  <td className="px-4 py-2.5">
                    {r.resiliation_en_cours ? (
                      <ResiliationSuivi row={r} />
                    ) : r.bail_id == null ? (
                      <span className="badge badge-neutral">
                        Aucun bail
                      </span>
                    ) : actifAuDossier ? (
                      <span className="badge badge-emerald">
                        Bail au dossier
                      </span>
                    ) : (
                      <span className="badge badge-amber">
                        Actif — PDF à importer
                      </span>
                    )}
                    {r.renouvellement_status &&
                    RENOUVELLEMENT_BADGES[r.renouvellement_status] ? (
                      <div className="mt-1">
                        <span
                          className={`badge ${RENOUVELLEMENT_BADGES[r.renouvellement_status].cls}`}
                        >
                          {
                            RENOUVELLEMENT_BADGES[r.renouvellement_status]
                              .label
                          }
                        </span>
                      </div>
                    ) : null}
                    {r.dossier_id != null &&
                    r.dossier_statut != null ? (
                      <div className="mt-1">
                        {/* Lecture seule — le statut de relocation se
                            MODIFIE à la source : le kanban Locations
                            (retour Phil 2026-08-13). */}
                        <RelocationStatutPastille
                          statut={r.dossier_statut}
                          dossierId={r.dossier_id}
                        />
                      </div>
                    ) : null}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <span className="inline-flex flex-wrap items-center justify-end gap-1.5">
                      {r.bail_id != null ? (
                        <>
                          {/* Ordre voulu par Phil (2026-08-14) :
                              Bail · Avis · Mettre fin (réduit) ·
                              Remplacer · + · poubelle. Avis et
                              Mettre fin s'intercalent DANS
                              BailDocActions via entreBoutons. */}
                          <BailDocActions
                            bailId={r.bail_id}
                            hasDoc={r.document_id != null}
                            exceptionMotif={r.sans_document_motif}
                            signedAt={r.signed_at}
                            compact
                            entreBoutons={
                              <>
                                {r.renouvellement_avis_document_id !=
                                null ? (
                                  <button
                                    type="button"
                                    onClick={() =>
                                      void ouvrirDoc(
                                        r.renouvellement_avis_document_id!
                                      )
                                    }
                                    className="inline-flex items-center gap-1.5 rounded-lg border border-white/15 bg-brand-950 px-2.5 py-1 text-xs font-semibold text-white/80 transition hover:border-white/30 hover:text-white"
                                    title="Ouvrir l'avis de renouvellement courant (PDF)"
                                  >
                                    <FileDown className="h-3.5 w-3.5" />
                                    Avis
                                  </button>
                                ) : null}
                                {r.resiliation_en_cours ? null : r
                                    .dossier_id != null ? (
                                  // Départ déjà acté : « Mettre fin
                                  // au bail » n'a plus d'effet et
                                  // laisse croire à une action. Le
                                  // geste utile ici, c'est l'inverse
                                  // — le locataire a changé d'idée
                                  // (retour Phil 2026-08-19).
                                  <button
                                    type="button"
                                    onClick={() => setAnnulerDepartFor(r)}
                                    className="inline-flex items-center rounded-lg border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[11px] font-semibold text-amber-300 transition hover:bg-amber-500/20"
                                    title="Le départ est déjà confirmé — annuler le départ si le locataire reste"
                                  >
                                    Annuler le départ
                                  </button>
                                ) : (
                                  <>
                                    <button
                                      type="button"
                                      onClick={() => setFinBailFor(r)}
                                      className="inline-flex items-center rounded-lg border border-rose-500/40 bg-rose-500/10 px-2 py-0.5 text-[11px] font-semibold text-rose-300 transition hover:bg-rose-500/20"
                                    >
                                      Mettre fin au bail
                                    </button>
                                    {/* Transfert d'unité (2026-09-09) :
                                        même geste partout où un bail
                                        actif s'affiche. */}
                                    <TransfertUniteButton
                                      bailId={r.bail_id}
                                      locataireNom={r.locataire_nom}
                                      immeubleId={r.immeuble_id}
                                      immeubleName={r.immeuble_name}
                                      logementNumero={r.logement_numero}
                                      loyerActuel={r.loyer_mensuel}
                                      finActuelle={r.date_fin}
                                      compact
                                      onDone={(msg) => {
                                        setFlash(msg);
                                        void load();
                                      }}
                                    />
                                  </>
                                )}
                              </>
                            }
                            onChanged={() => void load()}
                          />
                          <button
                            type="button"
                            onClick={() => setCreerFor(r)}
                            title="Préparer un NOUVEAU bail sur ce logement (prochain locataire)"
                            className="rounded-lg border border-brand-700 bg-brand-900 p-1.5 text-white/70 transition hover:bg-brand-800"
                          >
                            <Plus className="h-3 w-3" />
                          </button>
                          <button
                            type="button"
                            onClick={() => void supprimerBail(r)}
                            title="Supprimer ce bail (erreur de saisie) — pour une vraie fin de bail, utilise « Mettre fin au bail »"
                            className="rounded-lg border border-rose-500/40 bg-rose-500/10 p-1.5 text-rose-300 transition hover:bg-rose-500/20"
                          >
                            <Trash2 className="h-3 w-3" />
                          </button>
                        </>
                      ) : (
                        <>
                          {/* Un logement en RELOCATION se suit dans
                              Locations, et nulle part ailleurs :
                              créer un bail ici fabriquerait un
                              deuxième chemin qui contourne le
                              dossier et son garde-fou d'import
                              (retour Phil 2026-08-19). */}
                          {r.dossier_id != null ? (
                            <Link
                              href={
                                `/immobilier/locations?focus=${r.dossier_id}` as never
                              }
                              className="inline-flex items-center gap-1.5 rounded-lg border border-violet-400/40 bg-violet-500/10 px-2.5 py-1 text-xs font-semibold text-violet-200 transition hover:bg-violet-500/20"
                              title="Ce logement est en relocation — le bail se crée et s'importe depuis le dossier"
                            >
                              Suivre dans Locations →
                            </Link>
                          ) : (
                            <button
                              type="button"
                              onClick={() => setCreerFor(r)}
                              className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 text-xs font-semibold text-emerald-300 transition hover:bg-emerald-500/20"
                            >
                              <Plus className="h-3 w-3" /> Créer un
                              nouveau bail
                            </button>
                          )}
                          {r.prochain_bail_id != null ? (
                            <BailDocActions
                              bailId={r.prochain_bail_id}
                              hasDoc={r.prochain_document_id != null}
                              compact
                              onChanged={() => void load()}
                            />
                          ) : null}
                        </>
                      )}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {annulerDepartFor ? (
        <AnnulerDepartModal
          row={annulerDepartFor}
          onClose={() => setAnnulerDepartFor(null)}
          onDone={() => {
            setAnnulerDepartFor(null);
            void load();
          }}
        />
      ) : null}
      {finBailFor && finBailFor.bail_id != null ? (
        <FinBailModal
          bailId={finBailFor.bail_id}
          locataireNom={finBailFor.locataire_nom}
          immeubleName={finBailFor.immeuble_name}
          logementNumero={finBailFor.logement_numero}
          onClose={() => setFinBailFor(null)}
          onDone={(msg) => {
            setFinBailFor(null);
            setFlash(msg);
            void load();
          }}
        />
      ) : null}
      {creerFor ? (
        <CreerBailModal
          logementId={creerFor.logement_id}
          immeubleName={creerFor.immeuble_name}
          logementNumero={creerFor.logement_numero}
          logementEnChambres={creerFor.logement_en_chambres}
          onClose={() => setCreerFor(null)}
          onDone={(statut) => {
            setCreerFor(null);
            setFlash(
              statut === "actif"
                ? "Bail créé ACTIF (déjà en vigueur) — importe le PDF signé pour l'avoir au dossier."
                : "Bail créé (proposé) — importe le PDF signé (CORPIQ) pour le rendre actif."
            );
            void load();
          }}
        />
      ) : null}
    </>
  );
}
