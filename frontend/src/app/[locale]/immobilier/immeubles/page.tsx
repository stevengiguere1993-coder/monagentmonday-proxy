"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Building2,
  Loader2,
  Plus,
  Search,
  X
} from "lucide-react";

import { Link, useRouter } from "@/i18n/navigation";
import { authedFetch, getToken } from "@/lib/auth";
import { BoutonExport } from "@/components/immobilier/bouton-export";
import {
  EntrepriseSelector,
  ImmobilierTopbar,
  useImmobilierLayout
} from "../layout";

type ImmeubleListItem = {
  id: number;
  name: string;
  address: string;
  city?: string | null;
  type: string;
  nb_logements?: number | null;
  cover_photo_url?: string | null;
  has_cover_photo?: boolean;
  is_active: boolean;
  gestion_externe?: boolean;
  nb_logements_actifs: number;
  nb_logements_occupes: number;
  revenu_mensuel: number;
  taux_occupation: number;
};

const TYPES = [
  { value: "residentiel", label: "Résidentiel" },
  { value: "commercial", label: "Commercial" },
  { value: "mixte", label: "Mixte" },
  { value: "unifamilial", label: "Unifamilial" },
  { value: "autre", label: "Autre" }
];

function fmtCurrency(n: number): string {
  return new Intl.NumberFormat("fr-CA", {
    style: "currency",
    currency: "CAD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  }).format(n);
}

export default function ImmeublesListPage() {
  const router = useRouter();
  const {
    currentEntrepriseId,
    setCurrentEntrepriseId,
    entreprises,
    refreshEntreprises
  } = useImmobilierLayout();
  const [list, setList] = useState<ImmeubleListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);

  const currentEnt = entreprises.find((e) => e.id === currentEntrepriseId) || null;

  async function reload() {
    setError(null);
    try {
      const url =
        currentEntrepriseId != null
          ? `/api/v1/immobilier/immeubles?entreprise_id=${currentEntrepriseId}`
          : "/api/v1/immobilier/immeubles";
      const res = await authedFetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setList((await res.json()) as ImmeubleListItem[]);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  useEffect(() => {
    setList(null); // évite d'afficher l'ancienne liste pendant le swap
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentEntrepriseId]);

  const filtered = list
    ? list.filter((x) => {
        if (!search.trim()) return true;
        const q = search.toLowerCase();
        return (
          x.name.toLowerCase().includes(q) ||
          x.address.toLowerCase().includes(q) ||
          (x.city || "").toLowerCase().includes(q)
        );
      })
    : null;

  return (
    <>
      <ImmobilierTopbar
        breadcrumbs={[
          { label: "Gestion immobilière", href: "/immobilier" },
          { label: "Immeubles" }
        ]}
        rightSlot={
          <>
            <BoutonExport
              cibles={[
                {
                  base: "/api/v1/immobilier/exports/immeubles",
                  sujet: "immeubles",
                  params: { entreprise_id: currentEntrepriseId ?? undefined }
                }
              ]}
            />
            <button
              type="button"
              onClick={() => setShowCreate(true)}
              title={
                currentEnt
                  ? `Créer un immeuble pour ${currentEnt.name}`
                  : "Créer un immeuble (l'entreprise propriétaire se choisit dans le formulaire)"
              }
              className="btn-outline-accent btn-sm"
            >
              <Plus className="h-3.5 w-3.5" />
              Nouvel immeuble
            </button>
          </>
        }
      />

      {/* pb-28 : le contenu ne doit pas passer sous le bouton Aide flottant */}
      <div className="p-4 pb-28 lg:p-6 lg:pb-28">
        {currentEnt ? (
          <p className="mb-3 badge badge-sky">
            <Building2 className="h-3 w-3" />
            Suivi pour <strong className="text-white">{currentEnt.name}</strong> · les immeubles affichés sont ceux qu&apos;elle détient.
          </p>
        ) : (
          <p className="mb-3 badge badge-amber">
            <AlertTriangle className="h-3 w-3" />
            Aucune entreprise sélectionnée — sélectionne-en une à côté de la barre de recherche pour pouvoir ajouter un immeuble.
          </p>
        )}

        <div className="mb-4 flex flex-wrap items-center gap-2">
          <div className="relative w-full max-w-md flex-1 sm:w-auto">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/40" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Recherche par nom, adresse, ville…"
              className="input w-full pl-9"
            />
          </div>
          {/* Sélecteur d'entreprise active (déplacé de la sidebar) */}
          <EntrepriseSelector
            entreprises={entreprises}
            currentId={currentEntrepriseId}
            onChange={setCurrentEntrepriseId}
            onAdded={refreshEntreprises}
          />
          {filtered ? (
            <span className="text-xs text-white/50">
              {filtered.length} / {list?.length || 0}
            </span>
          ) : null}
        </div>

        {error ? (
          <p className="mb-4 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
            <AlertTriangle className="mr-1.5 inline h-3.5 w-3.5" />
            {error}
          </p>
        ) : null}

        {filtered === null ? (
          <div className="flex items-center gap-2 text-xs text-white/50">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Chargement…
          </div>
        ) : filtered.length === 0 ? (
          <p className="rounded-lg border border-brand-800 bg-brand-900 px-4 py-3 text-sm text-white/60">
            Aucun immeuble {search ? "correspondant" : "dans le portefeuille"}.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-2xl border border-brand-800 bg-brand-900">
            <table className="w-full min-w-[600px] text-left text-sm">
              <thead className="border-b border-brand-800 bg-brand-950 text-[10px] uppercase tracking-wider text-white/50">
                <tr>
                  <th className="px-4 py-2.5">Immeuble</th>
                  <th className="px-4 py-2.5">Type</th>
                  <th className="px-4 py-2.5 text-right">Logements</th>
                  <th className="px-4 py-2.5 text-right">Occupation</th>
                  <th className="px-4 py-2.5 text-right">Revenu/m</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-brand-800">
                {filtered.map((imm) => (
                  <tr key={imm.id} className="hover:bg-brand-950/50">
                    <td className="px-4 py-3">
                      <Link
                        // eslint-disable-next-line @typescript-eslint/no-explicit-any
                        href={`/immobilier/immeubles/${imm.id}` as any}
                        className="flex items-center gap-3"
                      >
                        <div className="h-10 w-10 flex-shrink-0 overflow-hidden rounded-lg bg-brand-950">
                          {imm.has_cover_photo || imm.cover_photo_url ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img
                              src={
                                imm.has_cover_photo
                                  ? `/api/v1/immobilier/immeubles/${imm.id}/cover-photo?t=${getToken() || ""}`
                                  : (imm.cover_photo_url as string)
                              }
                              alt=""
                              className="h-full w-full object-cover"
                            />
                          ) : (
                            <div className="flex h-full w-full items-center justify-center text-white/30">
                              <Building2 className="h-5 w-5" />
                            </div>
                          )}
                        </div>
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="truncate font-bold text-white">
                              {imm.name}
                            </span>
                            {imm.gestion_externe ? (
                              <span className="badge badge-sky flex-shrink-0">
                                Gestion externe
                              </span>
                            ) : null}
                          </div>
                          <div className="truncate text-[11px] text-white/50">
                            {imm.address}
                            {imm.city ? `, ${imm.city}` : ""}
                          </div>
                        </div>
                      </Link>
                    </td>
                    <td className="px-4 py-3 text-xs text-white/60">
                      {imm.type}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs">
                      {imm.nb_logements_actifs}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs">
                      <span
                        className={
                          imm.taux_occupation >= 0.9
                            ? "text-emerald-300"
                            : "text-amber-300"
                        }
                      >
                        {(imm.taux_occupation * 100).toFixed(0)}%
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs text-white/80">
                      {fmtCurrency(imm.revenu_mensuel)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {showCreate ? (
        <CreateImmeubleModal
          entrepriseId={currentEntrepriseId}
          entreprises={entreprises}
          onClose={() => setShowCreate(false)}
          onSaved={(createdId) => {
            setShowCreate(false);
            // Ouvrir directement la fiche du nouvel immeuble (retour Phil).
            router.push(`/immobilier/immeubles/${createdId}` as any);
          }}
        />
      ) : null}
    </>
  );
}

function ModalShell({
  title,
  onClose,
  children
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm">
      <div className="my-8 w-full max-w-lg rounded-2xl border border-brand-800 bg-brand-950 shadow-2xl">
        <div className="flex items-center justify-between border-b border-brand-800 px-5 py-3">
          <h2 className="text-sm font-bold uppercase tracking-wider text-accent-500">
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="btn-ghost btn-xs"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}

function CreateImmeubleModal({
  entrepriseId,
  entreprises,
  onClose,
  onSaved
}: {
  entrepriseId: number | null;
  entreprises: { id: number; name: string }[];
  onClose: () => void;
  onSaved: (createdId: number) => void;
}) {
  // Entreprise propriétaire choisie DANS le formulaire (pré-remplie si un
  // filtre entreprise est actif sur la page) — le bouton « Nouvel
  // immeuble » n'est plus jamais grisé (retour Phil 2026-07-10).
  const [entId, setEntId] = useState<number | null>(entrepriseId);
  const [form, setForm] = useState({
    address: "",
    city: "",
    postal_code: "",
    type: "residentiel",
    annee_construction: "",
    nb_logements: "",
    purchase_price: ""
  });
  const [gestionExterne, setGestionExterne] = useState(false);
  const [gestionNom, setGestionNom] = useState("");
  const [gestionContact, setGestionContact] = useState("");
  const [photoFile, setPhotoFile] = useState<File | null>(null);
  const [photoPreview, setPhotoPreview] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function onPhotoChange(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] || null;
    setPhotoFile(f);
    setPhotoPreview((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return f ? URL.createObjectURL(f) : null;
    });
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (entId == null) {
      setErr("Choisis l'entreprise propriétaire de l'immeuble.");
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      // Création de l'immeuble (sans nom — backend prend l'adresse).
      const body: Record<string, unknown> = {
        address: form.address.trim(),
        type: form.type,
        entreprise_id: entId // auto-rattache l'ownership 100%
      };
      if (form.city.trim()) body.city = form.city.trim();
      if (form.postal_code.trim()) body.postal_code = form.postal_code.trim();
      if (form.annee_construction)
        body.annee_construction = Number(form.annee_construction);
      if (form.nb_logements) body.nb_logements = Number(form.nb_logements);
      if (form.purchase_price)
        body.purchase_price = Number(form.purchase_price);
      if (gestionExterne) {
        body.gestion_externe = true;
        if (gestionNom.trim()) body.gestionnaire_externe_nom = gestionNom.trim();
        if (gestionContact.trim())
          body.gestionnaire_externe_contact = gestionContact.trim();
      }

      const res = await authedFetch("/api/v1/immobilier/immeubles", {
        method: "POST",
        body: JSON.stringify(body)
      });
      if (!res.ok) {
        const t = await res.text();
        throw new Error(t.slice(0, 240) || `HTTP ${res.status}`);
      }
      const created = (await res.json()) as { id: number };

      // Upload photo (best-effort) si fournie.
      if (photoFile && created.id) {
        const fd = new FormData();
        fd.append("file", photoFile);
        const up = await authedFetch(
          `/api/v1/immobilier/immeubles/${created.id}/cover-photo`,
          { method: "POST", body: fd }
        );
        if (!up.ok) {
          const t = await up.text();
          throw new Error(
            `Immeuble créé mais upload photo échoué : ${t.slice(0, 200)}`
          );
        }
      }

      if (photoPreview) URL.revokeObjectURL(photoPreview);
      onSaved(created.id);
    } catch (e2) {
      setErr((e2 as Error).message);
    } finally {
      setSaving(false);
    }
  }

  function set<K extends keyof typeof form>(k: K, v: string) {
    setForm({ ...form, [k]: v });
  }

  return (
    <ModalShell title="Nouvel immeuble" onClose={onClose}>
      <form onSubmit={submit} className="grid gap-4">
        <div>
          <label className="label">Entreprise propriétaire</label>
          <select
            required
            value={entId == null ? "" : String(entId)}
            onChange={(e) =>
              setEntId(e.target.value ? Number(e.target.value) : null)
            }
            className="input"
          >
            <option value="" disabled>
              Choisir l&apos;entreprise…
            </option>
            {entreprises.map((ent) => (
              <option key={ent.id} value={ent.id}>
                {ent.name}
              </option>
            ))}
          </select>
          <p className="mt-1 text-[10px] text-white/40">
            L&apos;immeuble lui sera rattaché à 100 % — les parts s&apos;ajustent
            ensuite dans la fiche immeuble, section Ownership.
          </p>
        </div>
        <div>
          <label className="label">Adresse</label>
          <input
            required
            value={form.address}
            onChange={(e) => set("address", e.target.value)}
            className="input"
            placeholder="1234 rue Notre-Dame Ouest"
          />
          <p className="mt-1 text-[10px] text-white/40">
            L&apos;immeuble est identifié par son adresse — pas besoin de nom.
          </p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="label">Ville</label>
            <input
              value={form.city}
              onChange={(e) => set("city", e.target.value)}
              className="input"
              placeholder="Montréal"
            />
          </div>
          <div>
            <label className="label">Code postal</label>
            <input
              value={form.postal_code}
              onChange={(e) => set("postal_code", e.target.value)}
              className="input font-mono"
              placeholder="H4C 1S9"
            />
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <div>
            <label className="label">Type</label>
            <select
              value={form.type}
              onChange={(e) => set("type", e.target.value)}
              className="input"
            >
              {TYPES.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">Année</label>
            <input
              type="number"
              value={form.annee_construction}
              onChange={(e) => set("annee_construction", e.target.value)}
              className="input font-mono"
              min={1700}
              max={2100}
            />
          </div>
          <div>
            <label className="label">Nb logements</label>
            <input
              type="number"
              value={form.nb_logements}
              onChange={(e) => set("nb_logements", e.target.value)}
              className="input font-mono"
              min={0}
            />
          </div>
        </div>
        <div>
          <label className="label">Prix d&apos;achat (CAD, optionnel)</label>
          <input
            type="number"
            value={form.purchase_price}
            onChange={(e) => set("purchase_price", e.target.value)}
            className="input font-mono"
            min={0}
            step={1000}
          />
        </div>

        <div className="rounded-lg border border-sky-400/30 bg-sky-500/10 p-3">
          <label className="flex cursor-pointer items-center gap-2 text-sm text-white">
            <input
              type="checkbox"
              checked={gestionExterne}
              onChange={(e) => setGestionExterne(e.target.checked)}
              className="h-4 w-4 accent-sky-400"
            />
            <span className="font-semibold">Immeuble en gestion externe</span>
          </label>
          <p className="mt-1 text-[11px] text-sky-200/70">
            Les paiements, renouvellements, dépôts et relances sont gérés par
            la compagnie de gestion — masqués dans Kratos.
          </p>
          {gestionExterne ? (
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <div>
                <label className="label">Compagnie de gestion</label>
                <input
                  value={gestionNom}
                  onChange={(e) => setGestionNom(e.target.value)}
                  className="input"
                  placeholder="ex. Gestion ABC inc."
                />
              </div>
              <div>
                <label className="label">Contact</label>
                <input
                  value={gestionContact}
                  onChange={(e) => setGestionContact(e.target.value)}
                  className="input"
                  placeholder="ex. 514 555-0123 / courriel"
                />
              </div>
            </div>
          ) : null}
        </div>

        <div>
          <label className="label">Photo de couverture (optionnelle)</label>
          <div className="flex items-center gap-3">
            {photoPreview ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={photoPreview}
                alt="Aperçu"
                className="h-16 w-16 rounded-lg object-cover"
              />
            ) : (
              <div className="flex h-16 w-16 items-center justify-center rounded-lg border border-dashed border-brand-700 bg-brand-950 text-white/30">
                <Building2 className="h-6 w-6" />
              </div>
            )}
            <div className="flex-1">
              <input
                type="file"
                accept="image/jpeg,image/png,image/webp,image/heic,image/heif"
                onChange={onPhotoChange}
                className="block w-full text-xs text-white/70 file:mr-3 file:rounded-lg file:border-0 file:bg-accent-500/15 file:px-3 file:py-1.5 file:text-xs file:font-semibold file:text-accent-500 hover:file:bg-accent-500/25"
              />
              <p className="mt-1 text-[10px] text-white/40">
                JPG, PNG, WEBP ou HEIC, max 8 Mo.
              </p>
            </div>
          </div>
        </div>

        {err ? (
          <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
            <AlertTriangle className="mr-1.5 inline h-3.5 w-3.5" />
            {err}
          </p>
        ) : null}

        <div className="flex items-center justify-end gap-2 border-t border-brand-800 pt-4">
          <button
            type="button"
            onClick={onClose}
            className="btn-secondary text-sm"
          >
            Annuler
          </button>
          <button
            type="submit"
            disabled={saving || !form.address.trim()}
            className="btn-accent inline-flex items-center text-sm disabled:opacity-60"
          >
            {saving ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Création…
              </>
            ) : (
              "Créer"
            )}
          </button>
        </div>
      </form>
    </ModalShell>
  );
}
