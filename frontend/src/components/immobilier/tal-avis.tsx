"use client";

/**
 * Menu « Générer ▾ » des lettres/avis TAL d'un bail — composant PARTAGÉ
 * (fiche immeuble, page Baux & paiements, hub locataire, page logement).
 * Les avis marqués `avecParams` ouvrent une modale qui collecte leurs
 * champs propres ; les mentions légales et délais sont bakés dans le PDF
 * (backend services/tal_forms.py).
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode
} from "react";
import {
  Eye,
  FileDown,
  Loader2,
  Mail,
  Pencil,
  Trash2,
  Upload,
  X
} from "lucide-react";

import { Link } from "@/i18n/navigation";
import { authedFetch } from "@/lib/auth";
import { BoutonExportZip } from "@/components/immobilier/bouton-export";
import { FinBailModal } from "@/components/immobilier/fin-bail";

export type BailDocument = {
  id: number;
  bail_id: number | null;
  locataire_id: number | null;
  logement_id?: number | null;
  type: string;
  titre: string;
  params: Record<string, unknown>;
  created_at: string | null;
  envoye_le: string | null;
  envoye_a: string | null;
  ouvert_le: string | null;
  signed_at: string | null;
  signed_by_name: string | null;
  /** 'genere' | 'importe' — importé = pièce déposée à la main. */
  source?: string;
  filename?: string | null;
  remplace_document_id?: number | null;
  /** false = simple communication (rappel, avis d'accès…). */
  signature_requise?: boolean;
};

/** Téléverse un document au dossier (bouton « Importer »). */
export async function importDocument(opts: {
  file: File;
  type?: string;
  titre?: string;
  bailId?: number;
  locataireId?: number;
  logementId?: number;
  immeubleId?: number;
}): Promise<BailDocument> {
  const fd = new FormData();
  fd.append("file", opts.file);
  fd.append("type", opts.type || "autre");
  if (opts.titre) fd.append("titre", opts.titre);
  if (opts.bailId != null) fd.append("bail_id", String(opts.bailId));
  if (opts.locataireId != null)
    fd.append("locataire_id", String(opts.locataireId));
  if (opts.logementId != null)
    fd.append("logement_id", String(opts.logementId));
  if (opts.immeubleId != null)
    fd.append("immeuble_id", String(opts.immeubleId));
  const r = await authedFetch("/api/v1/immobilier/documents/import", {
    method: "POST",
    body: fd
  });
  const d = await r.json().catch(() => null);
  if (!r.ok) {
    throw new Error((d && (d.detail || d.message)) || `Erreur ${r.status}`);
  }
  return d as BailDocument;
}

/** Bouton « Importer » réutilisable (input fichier caché). */
export function ImportDocButton({
  label,
  onPick,
  busy,
  title
}: {
  label: string;
  onPick: (file: File) => void;
  busy?: boolean;
  /** Infobulle : à quoi sert vraiment cet import. */
  title?: string;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf,image/jpeg,image/png"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onPick(f);
          e.target.value = "";
        }}
      />
      <button
        type="button"
        className="btn-secondary btn-xs"
        disabled={busy}
        title={title}
        onClick={() => inputRef.current?.click()}
      >
        {busy ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Upload className="h-3.5 w-3.5" />
        )}
        {label}
      </button>
    </>
  );
}

// Les composants d'une même ligne (Générer ▾ / Envoyer pour signature)
// se resynchronisent via cet événement quand un document est créé.
const DOCS_EVENT = "kratos:documents-changed";

function notifyDocumentsChanged(bailId: number): void {
  window.dispatchEvent(
    new CustomEvent(DOCS_EVENT, { detail: { bailId } })
  );
}

// Catalogue 2026-07-17 : les 5 premiers = formulaires OFFICIELS du TAL
// (PDF gouvernemental rempli tel quel) ; les 2 derniers = lettres maison
// envoyées par courriel SANS signature (avis de retard, avis d'accès).
const TAL_FORMS: {
  code: string;
  label: string;
  avecParams?: boolean;
  officiel?: boolean;
  sansSignature?: boolean;
}[] = [
  {
    code: "avis_modification",
    label: "Avis d'augmentation / modification (TAL-806)",
    avecParams: true,
    officiel: true
  },
  {
    code: "avis_non_reconduction",
    label: "Avis de non-reconduction — locataire (TAL-807)",
    avecParams: true,
    officiel: true
  },
  {
    code: "avis_reprise",
    label: "Avis de reprise de logement (TAL-809)",
    avecParams: true,
    officiel: true
  },
  {
    code: "avis_travaux_majeurs",
    label: "Avis de travaux majeurs (TAL-808)",
    avecParams: true,
    officiel: true
  },
  {
    code: "reponse_cession",
    label: "Réponse à une cession de bail (TAL-828)",
    avecParams: true,
    officiel: true
  },
  {
    code: "rappel_paiement",
    label: "Avis de retard de paiement",
    avecParams: true,
    sansSignature: true
  },
  {
    code: "avis_acces",
    label: "Avis d'accès au logement",
    avecParams: true,
    sansSignature: true
  },
  {
    code: "consentement_communications",
    label: "Consentement communications électroniques"
  }
];

// Types envoyés par simple courriel (PDF joint) — aucun flux de
// signature en ligne.
export const SANS_SIGNATURE = new Set([
  ...TAL_FORMS.filter((t) => t.sansSignature).map((t) => t.code),
  // Document personnalisé dont le modèle décoche « signature requise ».
  "personnalise_info"
]);

const MOI_MEME = new Set(["moi-même", "moi-meme", "moi même", "moi meme"]);

async function downloadTalPdf(
  bailId: number,
  code: string,
  body: Record<string, unknown>
): Promise<void> {
  const res = await authedFetch(
    `/api/v1/immobilier/baux/${bailId}/tal/${code}.pdf`,
    { method: "POST", body: JSON.stringify(body) }
  );
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${code.replace(/_/g, "-")}-bail-${bailId}.pdf`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  // Le backend a aussi CONSERVÉ le document — préviens le bouton
  // « Envoyer pour signature » de la même ligne.
  notifyDocumentsChanged(bailId);
}

// Modèles PERSONNALISÉS (règlement d'immeuble, contrat de chambreur…)
// créés dans Paramètres → Modèles de documents. Cache module (60 s) —
// le menu apparaît sur chaque ligne de bail, inutile de re-fetcher.
type PersoModele = {
  id: number;
  nom: string;
  titre: string | null;
  signature_requise: boolean;
  has_pdf: boolean;
};
let persoCache: { at: number; list: PersoModele[] } | null = null;
async function fetchPersoModeles(): Promise<PersoModele[]> {
  if (persoCache && Date.now() - persoCache.at < 60_000)
    return persoCache.list;
  try {
    const r = await authedFetch("/api/v1/immobilier/docs-perso/modeles");
    if (r.ok) {
      persoCache = {
        at: Date.now(),
        list: (await r.json()) as PersoModele[]
      };
    }
  } catch {
    /* silencieux — le menu TAL reste utilisable */
  }
  return persoCache?.list ?? [];
}

export function TalFormDropdown({ bailId }: { bailId: number }) {
  const [open, setOpen] = useState(false);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [paramsCode, setParamsCode] = useState<string | null>(null);
  const [perso, setPerso] = useState<PersoModele[] | null>(null);

  useEffect(() => {
    if (!open || perso !== null) return;
    void fetchPersoModeles().then(setPerso);
  }, [open, perso]);

  async function download(code: string) {
    setDownloading(code);
    try {
      await downloadTalPdf(bailId, code, {});
      setOpen(false);
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setDownloading(null);
    }
  }

  async function genererPerso(m: PersoModele) {
    setDownloading(`perso-${m.id}`);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/baux/${bailId}/docs-perso/${m.id}`,
        { method: "POST" }
      );
      if (!r.ok)
        throw new Error((await r.text()).slice(0, 200) || `HTTP ${r.status}`);
      notifyDocumentsChanged(bailId);
      setOpen(false);
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setDownloading(null);
    }
  }

  return (
    <div className="relative inline-block">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="btn-secondary btn-xs"
        title="Générer une lettre ou un avis TAL pour ce bail"
      >
        Générer ▾
      </button>
      {open ? (
        <div className="absolute right-0 z-30 mt-1 w-60 rounded-lg border border-brand-700 bg-brand-950 py-1 shadow-2xl">
          {/* Démêlage 2026-07-27 : « Générer ▾ » = documents à SIGNATURE
              seulement. Les avis courriel (retard, accès, assurance) se
              font depuis la page Communications — lien en bas du menu. */}
          {TAL_FORMS.filter((f) => !f.sansSignature).map((f) => (
            <button
              key={f.code}
              type="button"
              onClick={() => {
                if (f.avecParams) {
                  setParamsCode(f.code);
                  setOpen(false);
                } else {
                  void download(f.code);
                }
              }}
              disabled={downloading === f.code}
              className="block w-full px-3 py-1.5 text-left text-xs text-white/80 hover:bg-brand-900 hover:text-white disabled:opacity-50"
            >
              {downloading === f.code ? "Génération…" : f.label}
            </button>
          ))}
          {perso && perso.length > 0 ? (
            <>
              <div className="mx-3 my-1 border-t border-brand-800" />
              <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-white/40">
                Mes documents
              </div>
              {perso.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  onClick={() => void genererPerso(m)}
                  disabled={downloading === `perso-${m.id}`}
                  title={
                    m.signature_requise
                      ? "Généré puis envoyable pour signature en ligne"
                      : "Généré puis envoyable par courriel (suivi d'ouverture)"
                  }
                  className="block w-full px-3 py-1.5 text-left text-xs text-white/80 hover:bg-brand-900 hover:text-white disabled:opacity-50"
                >
                  {downloading === `perso-${m.id}`
                    ? "Génération…"
                    : m.titre || m.nom}
                </button>
              ))}
            </>
          ) : null}
          <div className="mx-3 my-1 border-t border-brand-800" />
          <Link
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            href={"/immobilier/communications" as any}
            className="block px-3 py-1.5 text-left text-xs text-accent-500 hover:bg-brand-900"
            title="Rappel de paiement, avis d'accès, demande d'assurance, message libre — envoyés par courriel depuis la page Communications"
          >
            Avis courriel (retard, accès…) → Communications
          </Link>
        </div>
      ) : null}
      {paramsCode ? (
        <TalAvisModal
          bailId={bailId}
          code={paramsCode}
          onClose={() => setParamsCode(null)}
        />
      ) : null}
    </div>
  );
}

/** Modale de paramètres pour les avis qui exigent des champs propres
 * (reprise, travaux majeurs, accès, réponse cession). */
function TalAvisModal({
  bailId,
  code,
  initialParams,
  onClose,
  onGenerated
}: {
  bailId: number;
  code: string;
  // Paramètres d'un document existant (« Modifier ») — régénère une
  // NOUVELLE version avec les champs préremplis.
  initialParams?: Record<string, unknown>;
  onClose: () => void;
  onGenerated?: () => void;
}) {
  const [f, setF] = useState<Record<string, string>>(() => {
    const base: Record<string, string> = {
      modif_mode: "nouveau_loyer",
      cession_decision: "accepte",
      travaux_evacuation: "non",
      travaux_duree_unite: "jours",
      reprise_pour: "moi"
    };
    for (const [k, v] of Object.entries(initialParams || {})) {
      if (v == null) continue;
      if (typeof v === "boolean") {
        base[k] = v ? "oui" : "non";
      } else {
        base[k] = String(v);
      }
    }
    // Normalisation des anciens documents (« Modifier » sur un doc créé
    // avant le passage aux formulaires officiels).
    if (base.mois_concerne && base.mois_concerne.length >= 7) {
      base.mois_concerne = base.mois_concerne.slice(0, 7);
    }
    if (!initialParams?.cession_decision && base.cession_accepte === "non") {
      base.cession_decision = "refus_serieux";
    }
    if (
      base.reprise_beneficiaire &&
      !MOI_MEME.has(base.reprise_beneficiaire.trim().toLowerCase())
    ) {
      base.reprise_pour = "proche";
    }
    return base;
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const set = (k: string) => (v: string) =>
    setF((prev) => ({ ...prev, [k]: v }));

  const titre =
    TAL_FORMS.find((t) => t.code === code)?.label || "Paramètres de l'avis";

  const valid = (() => {
    switch (code) {
      case "avis_modification": {
        const mode = f.modif_mode || "nouveau_loyer";
        if (mode === "nouveau_loyer") return !!f.nouveau_loyer;
        if (mode === "hausse_montant") return !!f.hausse_montant;
        return !!f.hausse_pct;
      }
      case "avis_non_reconduction":
        return true;
      case "rappel_paiement":
        return !!(f.montant_du && f.mois_concerne);
      case "avis_reprise":
        return (
          f.reprise_pour !== "proche" || !!f.reprise_beneficiaire?.trim()
        );
      case "avis_travaux_majeurs":
        return !!(f.travaux_description?.trim() && f.travaux_date_debut);
      case "avis_acces":
        return !!(f.acces_date && f.acces_motif?.trim());
      case "reponse_cession": {
        const d = f.cession_decision || "accepte";
        if (d === "accepte") return !!f.cession_date;
        if (d === "refus_autre")
          return !!(f.cession_date && f.cession_motif_refus?.trim());
        return !!f.cession_motif_refus?.trim();
      }
      default:
        return true;
    }
  })();

  async function generer() {
    if (!valid) return;
    setBusy(true);
    setErr(null);
    const num = (s?: string) =>
      s?.trim() ? Number(s.replace(/\s/g, "").replace(",", ".")) : null;
    const body: Record<string, unknown> = {};
    if (code === "avis_modification") {
      const mode = f.modif_mode || "nouveau_loyer";
      body.modif_mode = mode;
      if (mode === "nouveau_loyer") body.nouveau_loyer = num(f.nouveau_loyer);
      else if (mode === "hausse_montant")
        body.hausse_montant = num(f.hausse_montant);
      else body.hausse_pct = num(f.hausse_pct);
      body.nouvelle_date_debut = f.nouvelle_date_debut || null;
      body.nouvelle_date_fin = f.nouvelle_date_fin || null;
      body.motif = f.motif?.trim() || null;
    } else if (code === "avis_non_reconduction") {
      body.depart_date = f.depart_date || null;
    } else if (code === "rappel_paiement") {
      body.montant_du = num(f.montant_du);
      body.mois_concerne = f.mois_concerne
        ? `${f.mois_concerne.slice(0, 7)}-01`
        : null;
    } else if (code === "avis_reprise") {
      if (f.reprise_pour === "proche") {
        body.reprise_beneficiaire = f.reprise_beneficiaire?.trim();
        body.reprise_lien = f.reprise_lien?.trim() || null;
      } else {
        body.reprise_lien = "moi-même";
      }
      body.reprise_date = f.reprise_date || null;
    } else if (code === "avis_travaux_majeurs") {
      body.travaux_description = f.travaux_description?.trim();
      body.travaux_date_debut = f.travaux_date_debut;
      body.travaux_duree_valeur = f.travaux_duree_valeur?.trim() || null;
      body.travaux_duree_unite = f.travaux_duree_unite || "jours";
      body.travaux_evacuation = f.travaux_evacuation === "oui";
      if (f.travaux_evacuation === "oui") {
        body.travaux_evacuation_du = f.travaux_evacuation_du || null;
        body.travaux_evacuation_au = f.travaux_evacuation_au || null;
        body.travaux_indemnite = num(f.travaux_indemnite);
      }
      body.travaux_conditions = f.travaux_conditions?.trim() || null;
    } else if (code === "avis_acces") {
      body.acces_date = f.acces_date;
      body.acces_plage = f.acces_plage?.trim() || null;
      body.acces_motif = f.acces_motif?.trim();
    } else if (code === "reponse_cession") {
      const d = f.cession_decision || "accepte";
      body.cession_decision = d;
      body.cession_date = f.cession_date || null;
      body.cession_motif_refus =
        d === "accepte" ? null : f.cession_motif_refus?.trim();
    }
    try {
      await downloadTalPdf(bailId, code, body);
      onGenerated?.();
      onClose();
    } catch (e) {
      setErr(`Génération échouée : ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  const inputCls =
    "mt-0.5 block w-full rounded-md border border-brand-800 bg-brand-950 px-2 py-1.5 text-xs text-white outline-none focus:border-accent-500";
  const labelCls = "block text-[11px] font-semibold text-white/60";

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm">
      <div className="my-8 w-full max-w-md rounded-2xl border border-brand-800 bg-brand-950 shadow-2xl">
        <div className="flex items-center justify-between border-b border-brand-800 px-5 py-3">
          <h2 className="text-sm font-bold uppercase tracking-wider text-accent-500">
            {titre}
          </h2>
          <button type="button" onClick={onClose} className="btn-ghost btn-xs">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="grid gap-3 p-5">
          {code === "avis_modification" ? (
            <>
              <p className="text-xs text-white/50">
                Formulaire officiel TAL-806, prérempli avec le bail. À
                transmettre de 3 à 6 mois avant la fin du bail (12 mois
                et plus) ; le locataire a 1 mois pour répondre.
              </p>
              <label className={labelCls}>
                Forme de la hausse
                <select
                  value={f.modif_mode || "nouveau_loyer"}
                  onChange={(e) => set("modif_mode")(e.target.value)}
                  className={inputCls}
                >
                  <option value="nouveau_loyer" className="bg-brand-950 text-white">
                    Nouveau loyer ($ / mois)
                  </option>
                  <option value="hausse_montant" className="bg-brand-950 text-white">
                    Hausse en dollars (+ $ / mois)
                  </option>
                  <option value="hausse_pct" className="bg-brand-950 text-white">
                    Hausse en pourcentage (%)
                  </option>
                </select>
              </label>
              {(f.modif_mode || "nouveau_loyer") === "nouveau_loyer" ? (
                <label className={labelCls}>
                  Nouveau loyer mensuel ($) *
                  <input
                    inputMode="decimal"
                    value={f.nouveau_loyer || ""}
                    onChange={(e) => set("nouveau_loyer")(e.target.value)}
                    placeholder="ex. 1300"
                    className={inputCls}
                  />
                </label>
              ) : f.modif_mode === "hausse_montant" ? (
                <label className={labelCls}>
                  Montant de la hausse ($ / mois) *
                  <input
                    inputMode="decimal"
                    value={f.hausse_montant || ""}
                    onChange={(e) => set("hausse_montant")(e.target.value)}
                    placeholder="ex. 50"
                    className={inputCls}
                  />
                </label>
              ) : (
                <label className={labelCls}>
                  Pourcentage de la hausse (%) *
                  <input
                    inputMode="decimal"
                    value={f.hausse_pct || ""}
                    onChange={(e) => set("hausse_pct")(e.target.value)}
                    placeholder="ex. 4"
                    className={inputCls}
                  />
                </label>
              )}
              <div className="grid grid-cols-2 gap-3">
                <label className={labelCls}>
                  Bail renouvelé du
                  <input
                    type="date"
                    value={f.nouvelle_date_debut || ""}
                    onChange={(e) =>
                      set("nouvelle_date_debut")(e.target.value)
                    }
                    className={inputCls}
                  />
                </label>
                <label className={labelCls}>
                  au
                  <input
                    type="date"
                    value={f.nouvelle_date_fin || ""}
                    onChange={(e) =>
                      set("nouvelle_date_fin")(e.target.value)
                    }
                    className={inputCls}
                  />
                </label>
              </div>
              <p className="text-[10px] text-white/40">
                Laisse les dates vides pour reprendre automatiquement la
                durée du bail actuel.
              </p>
              <label className={labelCls}>
                Autre(s) modification(s) (garage, chauffage…)
                <textarea
                  value={f.motif || ""}
                  onChange={(e) => set("motif")(e.target.value)}
                  rows={2}
                  placeholder="Laisser vide si seule la hausse s'applique"
                  className={inputCls}
                />
              </label>
            </>
          ) : null}

          {code === "avis_non_reconduction" ? (
            <>
              <p className="text-xs text-white/50">
                Formulaire officiel TAL-807 — avis donné <b>par le
                locataire</b> qui quitte à la fin de son bail
                (art. 1946 C.c.Q.). Envoie-le-lui pour signature en
                ligne : c&apos;est lui qui le signe.
              </p>
              <label className={labelCls}>
                Date de départ (vide = fin du bail)
                <input
                  type="date"
                  value={f.depart_date || ""}
                  onChange={(e) => set("depart_date")(e.target.value)}
                  className={inputCls}
                />
              </label>
            </>
          ) : null}

          {code === "rappel_paiement" ? (
            <>
              <p className="text-xs text-white/50">
                Paiement exigé <b>immédiatement</b>. S&apos;envoie par
                courriel (PDF joint) — aucune signature requise.
              </p>
              <div className="grid grid-cols-2 gap-3">
                <label className={labelCls}>
                  Montant dû ($) *
                  <input
                    inputMode="decimal"
                    value={f.montant_du || ""}
                    onChange={(e) => set("montant_du")(e.target.value)}
                    placeholder="ex. 1250"
                    className={inputCls}
                  />
                </label>
                <label className={labelCls}>
                  Mois concerné *
                  <input
                    type="month"
                    value={f.mois_concerne || ""}
                    onChange={(e) => set("mois_concerne")(e.target.value)}
                    className={inputCls}
                  />
                </label>
              </div>
            </>
          ) : null}

          {code === "avis_reprise" ? (
            <>
              <p className="text-xs text-white/50">
                Formulaire officiel TAL-809. À transmettre au moins 6 mois
                avant la fin du bail ; le locataire a 1 mois pour répondre
                (silence = refus).
              </p>
              <label className={labelCls}>
                Le logement sera habité par
                <select
                  value={f.reprise_pour || "moi"}
                  onChange={(e) => set("reprise_pour")(e.target.value)}
                  className={inputCls}
                >
                  <option value="moi" className="bg-brand-950 text-white">
                    Moi-même (le locateur-propriétaire)
                  </option>
                  <option value="proche" className="bg-brand-950 text-white">
                    Un proche (parent, enfant…)
                  </option>
                </select>
              </label>
              {f.reprise_pour === "proche" ? (
                <div className="grid grid-cols-2 gap-3">
                  <label className={labelCls}>
                    Nom du bénéficiaire *
                    <input
                      value={f.reprise_beneficiaire || ""}
                      onChange={(e) =>
                        set("reprise_beneficiaire")(e.target.value)
                      }
                      placeholder="ex. Océane Meuser"
                      className={inputCls}
                    />
                  </label>
                  <label className={labelCls}>
                    Lien de parenté
                    <input
                      value={f.reprise_lien || ""}
                      onChange={(e) => set("reprise_lien")(e.target.value)}
                      placeholder="ex. ma conjointe, mon père…"
                      className={inputCls}
                    />
                  </label>
                </div>
              ) : null}
              <label className={labelCls}>
                Date de reprise (bail à durée indéterminée seulement)
                <input
                  type="date"
                  value={f.reprise_date || ""}
                  onChange={(e) => set("reprise_date")(e.target.value)}
                  className={inputCls}
                />
              </label>
              <p className="text-[10px] text-white/40">
                Bail à durée fixe : la date de fin du bail est reprise
                automatiquement sur le formulaire.
              </p>
            </>
          ) : null}

          {code === "avis_travaux_majeurs" ? (
            <>
              <p className="text-xs text-white/50">
                Formulaire officiel TAL-808. Préavis de 10 jours (3 mois
                si évacuation de plus de 7 jours).
              </p>
              <label className={labelCls}>
                Nature des travaux *
                <textarea
                  value={f.travaux_description || ""}
                  onChange={(e) =>
                    set("travaux_description")(e.target.value)
                  }
                  rows={3}
                  placeholder="ex. Réfection complète de la salle de bain"
                  className={inputCls}
                />
              </label>
              <div className="grid grid-cols-3 gap-3">
                <label className={labelCls}>
                  Date de début *
                  <input
                    type="date"
                    value={f.travaux_date_debut || ""}
                    onChange={(e) =>
                      set("travaux_date_debut")(e.target.value)
                    }
                    className={inputCls}
                  />
                </label>
                <label className={labelCls}>
                  Durée estimée
                  <input
                    inputMode="numeric"
                    value={f.travaux_duree_valeur || ""}
                    onChange={(e) =>
                      set("travaux_duree_valeur")(e.target.value)
                    }
                    placeholder="ex. 2"
                    className={inputCls}
                  />
                </label>
                <label className={labelCls}>
                  Unité
                  <select
                    value={f.travaux_duree_unite || "jours"}
                    onChange={(e) =>
                      set("travaux_duree_unite")(e.target.value)
                    }
                    className={inputCls}
                  >
                    <option value="jours" className="bg-brand-950 text-white">
                      jours
                    </option>
                    <option value="semaines" className="bg-brand-950 text-white">
                      semaines
                    </option>
                    <option value="mois" className="bg-brand-950 text-white">
                      mois
                    </option>
                  </select>
                </label>
              </div>
              <label className="flex cursor-pointer items-center gap-2 text-xs text-white/80">
                <input
                  type="checkbox"
                  checked={f.travaux_evacuation === "oui"}
                  onChange={(e) =>
                    set("travaux_evacuation")(
                      e.target.checked ? "oui" : "non"
                    )
                  }
                  className="h-3.5 w-3.5 accent-accent-500"
                />
                Évacuation temporaire requise
              </label>
              {f.travaux_evacuation === "oui" ? (
                <div className="grid grid-cols-3 gap-3">
                  <label className={labelCls}>
                    Évacuation du
                    <input
                      type="date"
                      value={f.travaux_evacuation_du || ""}
                      onChange={(e) =>
                        set("travaux_evacuation_du")(e.target.value)
                      }
                      className={inputCls}
                    />
                  </label>
                  <label className={labelCls}>
                    au
                    <input
                      type="date"
                      value={f.travaux_evacuation_au || ""}
                      onChange={(e) =>
                        set("travaux_evacuation_au")(e.target.value)
                      }
                      className={inputCls}
                    />
                  </label>
                  <label className={labelCls}>
                    Indemnité offerte ($)
                    <input
                      inputMode="decimal"
                      value={f.travaux_indemnite || ""}
                      onChange={(e) =>
                        set("travaux_indemnite")(e.target.value)
                      }
                      placeholder="0.00"
                      className={inputCls}
                    />
                  </label>
                </div>
              ) : null}
              <label className={labelCls}>
                Autres conditions (facultatif)
                <textarea
                  value={f.travaux_conditions || ""}
                  onChange={(e) =>
                    set("travaux_conditions")(e.target.value)
                  }
                  rows={2}
                  placeholder="ex. accès à l'eau coupé de 9 h à 12 h le premier jour"
                  className={inputCls}
                />
              </label>
            </>
          ) : null}

          {code === "avis_acces" ? (
            <>
              <p className="text-xs text-white/50">
                Préavis de 24 h — visite entre 9 h et 21 h (travaux :
                7 h à 19 h).
              </p>
              <div className="grid grid-cols-2 gap-3">
                <label className={labelCls}>
                  Date *
                  <input
                    type="date"
                    value={f.acces_date || ""}
                    onChange={(e) => set("acces_date")(e.target.value)}
                    className={inputCls}
                  />
                </label>
                <label className={labelCls}>
                  Plage horaire
                  <input
                    value={f.acces_plage || ""}
                    onChange={(e) => set("acces_plage")(e.target.value)}
                    placeholder="ex. entre 9 h et 12 h"
                    className={inputCls}
                  />
                </label>
              </div>
              <label className={labelCls}>
                Motif *
                <input
                  value={f.acces_motif || ""}
                  onChange={(e) => set("acces_motif")(e.target.value)}
                  placeholder="ex. vérification de l'état du logement, travaux mineurs…"
                  className={inputCls}
                />
              </label>
            </>
          ) : null}

          {code === "reponse_cession" ? (
            <>
              <p className="text-xs text-white/50">
                Formulaire officiel TAL-828 (avis reçus depuis le
                21 février 2024). Réponse à transmettre dans les
                15 jours — sans réponse, tu es réputé avoir consenti.
              </p>
              <label className={labelCls}>
                Décision
                <select
                  value={f.cession_decision || "accepte"}
                  onChange={(e) => set("cession_decision")(e.target.value)}
                  className={inputCls}
                >
                  <option value="accepte" className="bg-brand-950 text-white">
                    J&apos;accepte la cession de bail
                  </option>
                  <option
                    value="refus_serieux"
                    className="bg-brand-950 text-white"
                  >
                    Je refuse — motif sérieux (le bail continue)
                  </option>
                  <option
                    value="refus_autre"
                    className="bg-brand-950 text-white"
                  >
                    Je refuse — autre motif (le bail est résilié)
                  </option>
                </select>
              </label>
              {(f.cession_decision || "accepte") !== "refus_serieux" ? (
                <label className={labelCls}>
                  Date de cession (inscrite dans l&apos;avis du locataire) *
                  <input
                    type="date"
                    value={f.cession_date || ""}
                    onChange={(e) => set("cession_date")(e.target.value)}
                    className={inputCls}
                  />
                </label>
              ) : null}
              {(f.cession_decision || "accepte") !== "accepte" ? (
                <label className={labelCls}>
                  Motif du refus *
                  <textarea
                    value={f.cession_motif_refus || ""}
                    onChange={(e) =>
                      set("cession_motif_refus")(e.target.value)
                    }
                    rows={3}
                    placeholder="ex. capacité de payer insuffisante du candidat…"
                    className={inputCls}
                  />
                </label>
              ) : null}
            </>
          ) : null}

          {err ? (
            <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
              {err}
            </p>
          ) : null}

          <div className="flex items-center justify-end gap-2 border-t border-brand-800 pt-3">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="btn-secondary btn-sm"
            >
              Annuler
            </button>
            <button
              type="button"
              onClick={() => void generer()}
              disabled={busy || !valid}
              className="btn-accent btn-sm disabled:opacity-50"
            >
              {busy ? (
                <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              ) : (
                <FileDown className="mr-1 h-3.5 w-3.5" />
              )}
              Générer le PDF
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

const TYPES_AVEC_PARAMS = new Set(
  TAL_FORMS.filter((t) => t.avecParams).map((t) => t.code)
);

function fmtDateTime(iso: string | null): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString("fr-CA", {
      dateStyle: "short",
      timeStyle: "short"
    });
  } catch {
    return iso;
  }
}

/** Liste de documents RÉUTILISABLE (modale du bail + sections Documents
 * des fiches locataire/logement) : voir, modifier (nouvelle version),
 * envoyer (signature en ligne ou courriel PDF joint), supprimer. */
export function DocsList({
  docs,
  onChanged,
  emptyText
}: {
  docs: BailDocument[];
  onChanged: () => void;
  emptyText?: string;
}) {
  const [busyId, setBusyId] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [editDoc, setEditDoc] = useState<BailDocument | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  async function voir(d: BailDocument) {
    setBusyId(d.id);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/documents/${d.id}/pdf`
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank");
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (e) {
      setErr(`Ouverture échouée : ${(e as Error).message}`);
    } finally {
      setBusyId(null);
    }
  }

  async function envoyer(d: BailDocument) {
    const sansSig = SANS_SIGNATURE.has(d.type);
    if (
      !window.confirm(
        sansSig
          ? `Envoyer « ${d.titre} » au locataire par courriel (PDF joint) ?`
          : `Envoyer « ${d.titre} » au locataire pour signature en ligne ?`
      )
    )
      return;
    setBusyId(d.id);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/documents/${d.id}/${
          sansSig ? "envoyer-courriel" : "envoyer-signature"
        }`,
        { method: "POST", body: JSON.stringify({}) }
      );
      if (!r.ok)
        throw new Error(
          (await r.text()).slice(0, 200) || `HTTP ${r.status}`
        );
      const res = (await r.json()) as { envoye_a: string };
      setFlash(
        sansSig
          ? `Envoyé à ${res.envoye_a} (PDF joint).`
          : `Envoyé à ${res.envoye_a} — suivi d'ouverture actif.`
      );
      onChanged();
    } catch (e) {
      setErr(`Envoi échoué : ${(e as Error).message}`);
    } finally {
      setBusyId(null);
    }
  }

  async function supprimer(d: BailDocument) {
    if (
      !window.confirm(
        `Supprimer « ${d.titre} » ? (sa copie dans le Drive, s'il y en ` +
          `a une, sera mise à la corbeille)`
      )
    )
      return;
    setBusyId(d.id);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/documents/${d.id}`,
        { method: "DELETE" }
      );
      if (!r.ok && r.status !== 204)
        throw new Error(
          (await r.text()).slice(0, 200) || `HTTP ${r.status}`
        );
      onChanged();
    } catch (e) {
      setErr(`Suppression échouée : ${(e as Error).message}`);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-3">
      {flash ? (
            <p className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">
              {flash}
            </p>
          ) : null}
          {err ? (
            <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
              {err}
            </p>
          ) : null}

          {docs.length === 0 ? (
            <p className="rounded-xl border border-dashed border-brand-700 px-4 py-3 text-xs text-white/40">
              {emptyText ||
                "Aucun document — utilise « Générer ▾ » pour en créer un."}
            </p>
          ) : (
            <ul className="divide-y divide-brand-800 rounded-xl border border-brand-800">
              {docs.map((d) => (
                <li
                  key={d.id}
                  className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5"
                >
                  <span className="min-w-0">
                    <span className="text-sm font-medium text-white">
                      {d.titre}
                    </span>
                    {d.source === "importe" ? (
                      <span className="ml-2 rounded bg-sky-500/15 px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-sky-300">
                        importé
                      </span>
                    ) : SANS_SIGNATURE.has(d.type) ? (
                      <span className="ml-2 rounded bg-white/10 px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-white/50">
                        courriel
                      </span>
                    ) : null}
                    <span className="ml-2 text-[10px] text-white/40">
                      {fmtDateTime(d.created_at)}
                    </span>
                    <span className="mt-0.5 block text-[11px]">
                      {d.signed_at ? (
                        <span className="text-emerald-300">
                          Signé par {d.signed_by_name}{" "}
                          {fmtDateTime(d.signed_at)}
                        </span>
                      ) : d.ouvert_le ? (
                        <span className="text-sky-300">
                          Ouvert {fmtDateTime(d.ouvert_le)} — pas encore
                          signé
                        </span>
                      ) : d.envoye_le ? (
                        <span className="text-white/50">
                          Envoyé à {d.envoye_a} {fmtDateTime(d.envoye_le)}
                        </span>
                      ) : d.source === "importe" ? (
                        <span className="text-white/40">
                          {d.filename || "Fichier déposé au dossier"}
                        </span>
                      ) : (
                        <span className="text-white/40">
                          Brouillon — pas encore envoyé
                        </span>
                      )}
                    </span>
                  </span>
                  <span className="flex flex-shrink-0 items-center gap-1.5">
                    <button
                      type="button"
                      onClick={() => void voir(d)}
                      disabled={busyId === d.id}
                      className="btn-secondary btn-xs"
                      title="Voir le PDF"
                    >
                      <Eye className="h-3 w-3" />
                    </button>
                    {TYPES_AVEC_PARAMS.has(d.type) &&
                    !d.signed_at &&
                    d.source !== "importe" ? (
                      <button
                        type="button"
                        onClick={() => setEditDoc(d)}
                        disabled={busyId === d.id}
                        className="btn-secondary btn-xs"
                        title="Modifier (rouvre le formulaire prérempli — nouvelle version)"
                      >
                        <Pencil className="h-3 w-3" />
                      </button>
                    ) : null}
                    {!d.signed_at && d.source !== "importe" ? (
                      <button
                        type="button"
                        onClick={() => void envoyer(d)}
                        disabled={busyId === d.id}
                        className="btn-accent btn-xs"
                        title={
                          d.envoye_le
                            ? "Renvoyer pour signature"
                            : "Envoyer pour signature"
                        }
                      >
                        {busyId === d.id ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <Mail className="h-3 w-3" />
                        )}
                        {d.envoye_le ? "Renvoyer" : "Envoyer"}
                      </button>
                    ) : null}
                    {!d.signed_at ? (
                      <button
                        type="button"
                        onClick={() => void supprimer(d)}
                        disabled={busyId === d.id}
                        className="btn-outline-rose btn-xs"
                        title="Supprimer"
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                    ) : null}
                  </span>
                </li>
              ))}
            </ul>
          )}

      {editDoc && editDoc.bail_id != null ? (
        <TalAvisModal
          bailId={editDoc.bail_id}
          code={editDoc.type}
          initialParams={editDoc.params}
          onClose={() => setEditDoc(null)}
          onGenerated={onChanged}
        />
      ) : null}
    </div>
  );
}

/** Section « Documents » des fiches LOCATAIRE et LOGEMENT (retour Phil
 * 2026-07-20) : TOUT ce qui a été généré/envoyé (avis TAL, lettres…)
 * au même endroit, avec la génération par bail HORS tableau (le menu
 * « Générer ▾ » n'est plus coupé par un conteneur défilant). */
export function DocumentsSection({
  locataireId,
  logementId,
  bails
}: {
  locataireId?: number;
  logementId?: number;
  /** Baux depuis lesquels générer un document (libellé affiché si >1). */
  bails: { id: number; label: string }[];
}) {
  const [docs, setDocs] = useState<BailDocument[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);

  const load = useCallback(async () => {
    // categorie=dossier : uniquement les pièces SIGNÉES ou IMPORTÉES —
    // les simples communications vivent dans le journal, pas ici
    // (retour Phil 2026-07-27).
    const url =
      locataireId != null
        ? `/api/v1/immobilier/locataires/${locataireId}/documents?categorie=dossier`
        : `/api/v1/immobilier/logements/${logementId}/documents?categorie=dossier`;
    try {
      const r = await authedFetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setDocs((await r.json()) as BailDocument[]);
    } catch (e) {
      setErr(`Documents : ${(e as Error).message}`);
    }
  }, [locataireId, logementId]);

  const doImport = useCallback(
    async (file: File) => {
      setImporting(true);
      setErr(null);
      try {
        await importDocument({
          file,
          locataireId,
          logementId
        });
        await load();
      } catch (e) {
        setErr(`Import : ${(e as Error).message}`);
      } finally {
        setImporting(false);
      }
    },
    [locataireId, logementId, load]
  );

  useEffect(() => {
    void load();
    // Toute génération (même page ou ailleurs) rafraîchit la section.
    const handler = () => void load();
    window.addEventListener(DOCS_EVENT, handler);
    return () => window.removeEventListener(DOCS_EVENT, handler);
  }, [load]);

  return (
    <section className="rounded-2xl border border-brand-800 bg-brand-900 p-5">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-accent-500">
          Documents
        </h2>
        <span className="text-[11px] text-white/40">
          {docs ? `${docs.length} document${docs.length > 1 ? "s" : ""}` : ""}
        </span>
        <span className="ml-auto flex flex-wrap items-center gap-2">
          <ImportDocButton
            label="Importer"
            busy={importing}
            onPick={(f) => void doImport(f)}
          />
          {/* Zip de ce qui est listé ici (pièces du DOSSIER : signées ou
              importées) + index.csv. */}
          {docs && docs.length > 0 ? (
            <BoutonExportZip
              path={
                locataireId != null
                  ? `/api/v1/immobilier/locataires/${locataireId}/documents.zip?categorie=dossier`
                  : `/api/v1/immobilier/logements/${logementId}/documents.zip?categorie=dossier`
              }
              sujet={
                locataireId != null
                  ? `locataire_${locataireId}`
                  : `logement_${logementId}`
              }
              onError={(msg) => setErr(`Export : ${msg}`)}
            />
          ) : null}
          {bails.map((b) => (
            <span key={b.id} className="inline-flex items-center gap-1.5">
              {bails.length > 1 ? (
                <span className="text-[11px] text-white/50">{b.label}</span>
              ) : null}
              <TalFormDropdown bailId={b.id} />
            </span>
          ))}
        </span>
      </div>
      {bails.length === 0 ? (
        <div className="mb-3 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs text-amber-200">
          {locataireId != null ? (
            <>
              <b className="text-white">
                Ce locataire n&apos;a aucun bail
              </b>{" "}
              — les avis TAL se préremplissent depuis un bail, donc rien à
              générer ici pour l&apos;instant. Crée son bail depuis la
              fiche de l&apos;immeuble (onglet Baux &amp; locataires) et
              le menu « Générer ▾ » apparaîtra. Si ce locataire est un
              doublon (son bail vit sur une autre fiche), la page{" "}
              <Link
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                href={"/immobilier/locataires" as any}
                className="underline hover:text-white"
              >
                Locataires
              </Link>{" "}
              montre qui habite où — supprime le doublon avec la
              poubelle.
            </>
          ) : (
            <>
              <b className="text-white">Aucun bail actif</b> — logement
              libre : les avis se génèrent depuis un bail. L&apos;historique
              des documents des anciens baux reste visible ci-dessous.
            </>
          )}
        </div>
      ) : null}
      {err ? (
        <p className="mb-3 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          {err}
        </p>
      ) : null}
      {docs === null ? (
        <p className="flex items-center gap-2 text-xs text-white/50">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Chargement…
        </p>
      ) : (
        <DocsList
          docs={docs}
          onChanged={() => void load()}
          emptyText="Aucun document au dossier — importe un fichier (bouton « Importer ») ou génère un avis avec « Générer ▾ ». Les documents signés en ligne arrivent ici automatiquement."
        />
      )}
    </section>
  );
}

/**
 * « LE bail » d'une ligne : bouton qui OUVRE le bail courant (importé,
 * sinon signé en ligne) + bouton Importer/Remplacer. Remplacer archive
 * l'ancien dans les Documents (retour Phil 2026-07-27).
 */
/**
 * Badge-interrupteur « Au mois » d'un bail (chambres — retour Phil
 * 2026-07-28) : reconduction automatique au même prix, jamais d'avis de
 * renouvellement, loyer qui court jusqu'au départ. Cliquer bascule le
 * mode (PATCH /baux/{id}) — visible sur la fiche logement et la fiche
 * locataire, changeable en tout temps.
 */
export function AuMoisToggle({
  bailId,
  auMois,
  onChanged
}: {
  bailId: number;
  auMois: boolean;
  onChanged?: () => void;
}) {
  const [busy, setBusy] = useState(false);

  async function basculer() {
    const msg = auMois
      ? "Repasser ce bail en bail à durée fixe ? Il réapparaîtra dans le suivi des renouvellements."
      : "Passer ce bail « au mois » ? Reconduction automatique au même prix : plus d'avis de renouvellement, le loyer court jusqu'au départ.";
    if (!window.confirm(msg)) return;
    setBusy(true);
    try {
      const r = await authedFetch(`/api/v1/immobilier/baux/${bailId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ au_mois: !auMois })
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      onChanged?.();
    } catch {
      window.alert("Changement impossible — réessaie.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      type="button"
      disabled={busy}
      onClick={() => void basculer()}
      title={
        auMois
          ? "Bail au mois : reconduction auto, pas d'avis de renouvellement — cliquer pour repasser à durée fixe"
          : "Bail à durée fixe (suivi des renouvellements) — cliquer pour passer au mois (chambres)"
      }
      className={
        auMois
          ? "inline-flex items-center gap-1 rounded-full border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 text-[11px] font-semibold text-sky-300 transition hover:bg-sky-500/20 disabled:opacity-50"
          : "inline-flex items-center gap-1 rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-[11px] font-medium text-white/50 transition hover:text-white disabled:opacity-50"
      }
    >
      {auMois ? "Au mois ✓" : "Au mois ?"}
    </button>
  );
}

/**
 * « Résilier » depuis les fiches (locataire, logement) — simple
 * DÉCLENCHEUR du MÊME modal que la page Baux (FinBailModal) : 2 modes
 * visibles partout — entente de résiliation signée en ligne OU fin
 * immédiate sans avis (directive « miroir bidirectionnel »).
 */
export function ResilierBailButton({
  bailId,
  locataireNom,
  immeubleName,
  logementNumero,
  onChanged,
  onMessage
}: {
  bailId: number;
  locataireNom?: string | null;
  immeubleName?: string | null;
  logementNumero?: string | null;
  onChanged?: () => void;
  /** Reçoit le message de confirmation (entente envoyée / bail terminé). */
  onMessage?: (msg: string) => void;
}) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title="Mettre fin au bail — entente de résiliation signée en ligne OU fin immédiate sans avis"
        className="inline-flex items-center gap-1 rounded-full border border-rose-500/30 bg-rose-500/5 px-2.5 py-1 text-[11px] font-medium text-rose-300/80 transition hover:bg-rose-500/15 hover:text-rose-300 disabled:opacity-50"
      >
        Résilier
      </button>
      {open ? (
        <FinBailModal
          bailId={bailId}
          locataireNom={locataireNom}
          immeubleName={immeubleName}
          logementNumero={logementNumero}
          onClose={() => setOpen(false)}
          onDone={(msg) => {
            setOpen(false);
            onMessage?.(msg);
            onChanged?.();
          }}
        />
      ) : null}
    </>
  );
}


export function BailDocActions({
  bailId,
  hasDoc,
  signedAt,
  allowImportInitial = true,
  compact = false,
  entreBoutons,
  exceptionMotif,
  onChanged
}: {
  bailId: number;
  /** bail.document_id présent (un bail importé existe). */
  hasDoc: boolean;
  /** Bail signé en ligne (le PDF régénéré sert de repli). */
  signedAt?: string | null;
  /** false = pas d'import INITIAL ici (remplacer / voir seulement) —
   *  le bail initial s'importe depuis le kanban Locations. */
  allowImportInitial?: boolean;
  /** true = bouton d'import en ICÔNE seule (pages denses). */
  compact?: boolean;
  /** Boutons à intercaler entre « Bail » et « Remplacer » — l'ordre
   *  voulu par Phil sur la page Baux (2026-08-14) : Bail · Avis ·
   *  Mettre fin · Remplacer. */
  entreBoutons?: ReactNode;
  /** Motif d'EXCEPTION déjà déclaré (« aucun bail à joindre »). Quand
   *  il est fourni, le bouton d'exception apparaît ici — c'est-à-dire
   *  là où le bail vit, et non dans une alerte : une alerte y mène, elle
   *  n'agit pas (règle Phil 2026-08-19). */
  exceptionMotif?: string | null;
  onChanged?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ouvrable = hasDoc || Boolean(signedAt);
  const [saisieException, setSaisieException] = useState(false);
  const [motif, setMotif] = useState("");

  async function declarerException() {
    const m = motif.trim();
    if (m.length < 3) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/baux/${bailId}/exception-document`,
        { method: "POST", body: JSON.stringify({ motif: m }) }
      );
      if (!r.ok) throw new Error((await r.text()).slice(0, 160));
      setSaisieException(false);
      setMotif("");
      onChanged?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function retirerException() {
    setBusy(true);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/baux/${bailId}/exception-document`,
        { method: "DELETE" }
      );
      if (!r.ok) throw new Error((await r.text()).slice(0, 160));
      onChanged?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function ouvrir() {
    setBusy(true);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/baux/${bailId}/document`
      );
      if (!r.ok) {
        const d = await r.json().catch(() => null);
        throw new Error(
          (d && (d.detail || d.message)) || `HTTP ${r.status}`
        );
      }
      const url = URL.createObjectURL(await r.blob());
      window.open(url, "_blank");
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // Import en 2 temps (retour Phil 2026-07-27) : on choisit le fichier,
  // PUIS un mini-modal demande la date d'entrée en vigueur — elle donne
  // le titre « Bail signé 2026-07-01 » dans les Documents.
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [dateEntree, setDateEntree] = useState("");
  const [importAuMois, setImportAuMois] = useState(false);

  function demanderDate(file: File) {
    if (
      hasDoc &&
      !window.confirm(
        "Remplacer le bail ? L'ancien reste conservé dans les Documents du logement et du locataire — seul celui qui s'ouvre au clic change."
      )
    )
      return;
    setDateEntree("");
    setImportAuMois(false);
    setPendingFile(file);
  }

  async function remplacer(file: File, date: string, auMois: boolean) {
    setBusy(true);
    setErr(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      if (date) fd.append("date_entree", date);
      // « Au mois » demandé au même moment que la date (retour Phil
      // 2026-07-28) — coché seulement, on n'écrase pas un réglage
      // existant quand la case reste vide.
      if (auMois) fd.append("au_mois", "true");
      const r = await authedFetch(
        `/api/v1/immobilier/baux/${bailId}/document`,
        { method: "POST", body: fd }
      );
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        throw new Error(
          (d && (d.detail || d.message)) || `HTTP ${r.status}`
        );
      }
      notifyDocumentsChanged(bailId);
      onChanged?.();
      // C'est ICI que Phil voulait le consentement : « faudrait que ce
      // soit directement quand le bail est signé qu'on envoie ça ».
      // Le bail signé vient d'arriver au dossier — c'est le seul moment
      // où on y pense naturellement. Proposé, jamais automatique : rien
      // ne part vers un locataire sans un clic.
      setProposerConsentement(true);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const [proposerConsentement, setProposerConsentement] = useState(false);
  const [envoiConsent, setEnvoiConsent] = useState(false);

  async function envoyerConsentement() {
    setEnvoiConsent(true);
    setErr(null);
    try {
      const r = await authedFetch(
        `/api/v1/immobilier/baux/${bailId}/consentement/envoyer`,
        { method: "POST" }
      );
      if (!r.ok) throw new Error((await r.text()).slice(0, 200));
      setProposerConsentement(false);
      onChanged?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setEnvoiConsent(false);
    }
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      {proposerConsentement ? (
        <div
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4"
          onClick={() => setProposerConsentement(false)}
        >
          <div
            className="w-full max-w-sm rounded-2xl border border-brand-800 bg-brand-900 p-5 shadow-card"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-bold text-white">
              Bail importé — et le consentement ?
            </h3>
            <p className="mt-1 text-xs text-white/60">
              Sans consentement aux communications électroniques, les
              avis doivent partir <b>par la poste</b>. C&apos;est le bon
              moment pour le demander : le locataire vient de signer.
            </p>
            <p className="mt-2 text-[11px] text-white/40">
              Il peut refuser — son refus sera enregistré et le suivi le
              montrera.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setProposerConsentement(false)}
                className="btn-ghost btn-xs"
              >
                Plus tard
              </button>
              <button
                type="button"
                disabled={envoiConsent}
                onClick={() => void envoyerConsentement()}
                className="btn-accent btn-sm disabled:opacity-60"
              >
                {envoiConsent ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : null}
                Envoyer le consentement
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {ouvrable ? (
        <button
          type="button"
          className="btn-secondary btn-xs"
          disabled={busy}
          onClick={() => void ouvrir()}
          title="Ouvrir le bail (PDF)"
        >
          {busy ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <FileDown className="h-3 w-3" />
          )}
          Bail
        </button>
      ) : null}
      {entreBoutons ?? null}
      {hasDoc || allowImportInitial ? (
        /* Retour Phil 2026-08-19 : « le bouton importer c'était
           peut-être juste pour les unités déjà créées, quand par
           exemple je crée un immeuble et je veux juste venir rattacher
           le bail — faudrait trouver une façon que ce soit pas
           mélangeant avec les unités vacantes ». Exactement : il sert à
           JOINDRE le bail d'un locataire déjà en place, pas à louer.
           Louer une unité vacante passe par Locations. Le libellé et
           l'infobulle le disent maintenant. */
        <ImportDocButton
          label={
            compact
              ? ""
              : hasDoc
                ? "Remplacer"
                : "Joindre le bail signé"
          }
          title={
            hasDoc
              ? "Remplacer le PDF du bail au dossier"
              : "Joindre le bail d'un locataire DÉJÀ en place (rachat d'immeuble, régularisation). Pour louer une unité vacante, passe par Locations."
          }
          busy={busy}
          onPick={demanderDate}
        />
      ) : null}
      {/* Exception « aucun bail à joindre » — ici, là où le bail vit.
          Sans objet si un document est déjà au dossier. */}
      {!hasDoc && !signedAt ? (
        exceptionMotif ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => void retirerException()}
            className="btn-ghost btn-xs"
            title={`Exception déclarée : « ${exceptionMotif} » — cliquer pour la retirer`}
          >
            Exception ✕
          </button>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => setSaisieException(true)}
            className="btn-ghost btn-xs"
            title="Déclarer qu'il n'y a aucun bail à joindre à ce dossier"
          >
            Exception
          </button>
        )
      ) : null}
      {saisieException ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setSaisieException(false)}
        >
          <div
            className="w-full max-w-sm rounded-2xl border border-brand-800 bg-brand-900 p-5 shadow-card"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-bold text-amber-200">
              ⚠️ Aucun bail à joindre
            </h3>
            <p className="mt-1 text-xs text-white/55">
              Sans bail au dossier, tu n&apos;as aucune preuve du loyer ni
              des conditions convenues. Le motif reste au dossier, avec
              ton nom et la date.
            </p>
            <input
              type="text"
              value={motif}
              onChange={(e) => setMotif(e.target.value)}
              maxLength={255}
              placeholder="Pourquoi n'y a-t-il pas de bail ? (obligatoire)"
              className="input mt-2 w-full text-xs"
            />
            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setSaisieException(false)}
                className="btn-ghost btn-xs"
              >
                Annuler
              </button>
              <button
                type="button"
                disabled={busy || motif.trim().length < 3}
                onClick={() => void declarerException()}
                className="btn-secondary btn-sm disabled:opacity-50"
              >
                Déclarer l&apos;exception
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {err ? (
        <span className="text-[10px] text-rose-300" title={err}>
          {err.slice(0, 60)}
        </span>
      ) : null}
      {pendingFile ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setPendingFile(null)}
        >
          <div
            className="w-full max-w-xs rounded-2xl border border-brand-800 bg-brand-900 p-5 shadow-card"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-bold text-white">
              Date d&apos;entrée en vigueur du bail
            </h3>
            <p className="mt-1 text-xs text-white/50">
              Affichée dans les Documents : « Bail signé 2026-07-01 ».
              Laisse vide pour reprendre la date de début du bail.
            </p>
            <input
              type="date"
              value={dateEntree}
              onChange={(e) => setDateEntree(e.target.value)}
              className="input mt-3 w-full"
            />
            <label className="mt-3 flex cursor-pointer items-start gap-2">
              <input
                type="checkbox"
                checked={importAuMois}
                onChange={(e) => setImportAuMois(e.target.checked)}
                className="mt-0.5 h-4 w-4 accent-[var(--accent-500,#f59e0b)]"
              />
              <span className="text-xs text-white/60">
                <span className="font-semibold text-white">Bail au mois</span>{" "}
                (chambre) — reconduction automatique, jamais d&apos;avis de
                renouvellement.
              </span>
            </label>
            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                className="btn-secondary btn-xs"
                onClick={() => setPendingFile(null)}
              >
                Annuler
              </button>
              <button
                type="button"
                className="btn-accent btn-xs"
                disabled={busy}
                onClick={() => {
                  const f = pendingFile;
                  setPendingFile(null);
                  if (f) void remplacer(f, dateEntree, importAuMois);
                }}
              >
                {busy ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : null}
                Importer
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </span>
  );
}
