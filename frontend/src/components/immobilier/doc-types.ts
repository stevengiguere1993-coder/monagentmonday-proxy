/**
 * Types de documents locatifs NORMALISÉS — miroir TS de `IMM_DOC_TYPES`
 * (backend/app/api/v1/endpoints/immobilier_documents.py). Garder les
 * deux listes alignées.
 *
 * Retour Phil 2026-09-09 : à la création d'un locataire, le gestionnaire
 * veut déposer PLUSIEURS pièces (règlements de l'immeuble, assurance…),
 * pas seulement le bail. La liste sert aux menus d'import ; le backend
 * accepte toujours un type hors liste (releve31, dpa, avis générés…).
 */

export const IMM_DOC_TYPES: readonly { value: string; label: string }[] = [
  { value: "bail", label: "Bail" },
  { value: "reglement_immeuble", label: "Règlements de l'immeuble" },
  { value: "assurance", label: "Preuve d'assurance" },
  { value: "enquete_credit", label: "Enquête de crédit / références" },
  { value: "piece_identite", label: "Pièce d'identité" },
  { value: "autre", label: "Autre" }
];

const LABELS: Record<string, string> = Object.fromEntries(
  IMM_DOC_TYPES.map((t) => [t.value, t.label])
);

/** Libellé lisible d'un type de document (clé inconnue → telle quelle). */
export function docTypeLabel(type: string | null | undefined): string {
  if (!type) return "";
  return LABELS[type] ?? type;
}

/** Vrai si le type figure dans la liste normalisée. */
export function estDocTypeNormalise(type: string): boolean {
  return type in LABELS;
}

/** Un fichier choisi dans une zone d'import, avec le type à lui donner. */
export type FichierAImporter = {
  /** Clé stable pour React (nom + taille + horodatage du choix). */
  key: string;
  file: File;
  type: string;
};

/**
 * Type par défaut d'un fichier qui s'ajoute à une liste : « Bail » pour
 * le PREMIER PDF (s'il n'y a pas déjà un bail dans la liste), « Autre »
 * ensuite — c'est ce que le gestionnaire dépose neuf fois sur dix en
 * premier.
 */
export function docTypeParDefaut(
  file: File,
  dejaChoisis: readonly FichierAImporter[]
): string {
  const estPdf =
    file.type === "application/pdf" || /\.pdf$/i.test(file.name);
  const bailDeja = dejaChoisis.some((f) => f.type === "bail");
  return estPdf && !bailDeja ? "bail" : "autre";
}

/** Construit les entrées d'une FileList / d'un tableau de File (le
 *  type par défaut se décide fichier par fichier, dans l'ordre). */
export function versFichiersAImporter(
  files: Iterable<File>,
  dejaChoisis: readonly FichierAImporter[] = []
): FichierAImporter[] {
  const out: FichierAImporter[] = [];
  const tous = [...dejaChoisis];
  const stamp = Date.now();
  let i = 0;
  for (const file of files) {
    const entree: FichierAImporter = {
      key: `${file.name}-${file.size}-${stamp}-${i++}`,
      file,
      type: docTypeParDefaut(file, tous)
    };
    out.push(entree);
    tous.push(entree);
  }
  return out;
}
