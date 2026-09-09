"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Building2,
  Check,
  ChevronDown,
  Eye,
  Filter,
  Loader2,
  Mail,
  Plus,
  Search,
  Send,
  Settings,
  Star,
  Trash2,
  Users,
  X
} from "lucide-react";

import { useSearchParams } from "next/navigation";

import { authedFetch, hasMinRole } from "@/lib/auth";
import { useCurrentUser } from "@/hooks/use-current-user";
import { ImmobilierTopbar } from "../layout";

/**
 * Communications (gestion locative) — retour Phil 2026-07-27.
 *
 * Tout ce qui PART vers les locataires sans exiger de signature :
 * 1. À QUI — sélection PAR LOCATAIRE, immeuble par immeuble : la case
 *    d'un immeuble est tri-état (tous / partiel / aucun) et coche ses
 *    locataires d'un coup ; un filtre (nom, courriel, logement,
 *    immeuble) réduit et déplie la liste. L'envoi reste UN courriel
 *    individualisé par locataire, jamais de liste visible.
 *    Les immeubles en GESTION EXTERNE ne sont jamais listés (retour
 *    Phil 2026-08-13 : « comment ça le 1-3-5 Elgin y apparaît ? »).
 * 2. QUOI — avis « modèle courriel » (rappel de paiement, avis d'accès,
 *    demande d'assurance) ou message libre. Le relevé 31 reste dans
 *    Suivis annuels ; les avis TAL à signature restent dans Documents.
 * 3. DE QUI — expéditeur (boîte M365), nom affiché et adresse de
 *    réponse (peut être externe, ex. gmail du gestionnaire) ; défauts
 *    enregistrables, modifiables à chaque envoi.
 * 4. AUDIT — journal filtrable de tout ce qui est parti ; chaque envoi
 *    apparaît aussi dans la fiche du locataire (section Communications).
 */

type Destinataire = {
  locataire_id: number;
  bail_id: number;
  nom: string;
  email?: string | null;
  logement?: string | null;
  //: Dû du mois courant (loyer + frais − payé) — bouton « Retards ».
  du_mois?: number;
};

type ImmeubleBloc = {
  immeuble_id: number;
  immeuble_name: string;
  locataires: Destinataire[];
};

type ProfilEnvoi = {
  label: string;
  from_email: string;
  from_name: string;
  reply_to: string;
};

type Reglages = {
  from_email: string;
  from_name: string;
  reply_to: string;
  // Profils d'expéditeurs approuvés (cas « deux gestionnaires ») —
  // sélectionnables par tous, gérés par les managers.
  profils: ProfilEnvoi[];
  // Label du profil pré-sélectionné pour tous (choisi par le manager).
  profil_defaut: string;
};

type EnvoiResultat = {
  envoyes: number;
  ignores_payes: string[];
  sans_email: string[];
  echecs: string[];
};

type AuditRow = {
  id: number;
  type: string;
  sujet: string;
  corps: string;
  locataire_id?: number | null;
  locataire_nom?: string | null;
  immeuble_id?: number | null;
  immeuble_nom?: string | null;
  destinataire_email: string;
  from_email?: string | null;
  from_name?: string | null;
  reply_to?: string | null;
  statut: string;
  created_by_email?: string | null;
  created_by_nom?: string | null;
  document_id?: number | null;
  document_ouvert_le?: string | null;
  document_signe_le?: string | null;
  document_signe_par?: string | null;
  document_signature_requise?: boolean;
  created_at?: string | null;
};

//: Corps de départ du message libre — {locateur} (le « De qui », ex.
//: « Kyle Brown - Gestion locative ») sert de signature ; effaçable.
const TEMPLATE_LIBRE = "Bonjour {locataire},\n\n\n\n{locateur}";

const TYPES = [
  {
    value: "rappel_paiement",
    label: "Rappel de paiement",
    desc: "Loyer impayé — le montant dû est calculé PAR locataire ; ceux qui ont payé le mois sont sautés automatiquement."
  },
  {
    value: "avis_acces",
    label: "Avis d'accès au logement",
    desc: "Visite/travaux — mêmes date, plage et motif pour tous les logements choisis."
  },
  {
    value: "demande_assurance",
    label: "Demande de preuve d'assurance",
    desc: "Demande la preuve d'assurance habitation à jour."
  },
  {
    value: "libre",
    label: "Message libre",
    desc: "Ton propre sujet et texte — {locataire}, {adresse}, {logement} et {locateur} sont remplacés pour chacun."
  }
] as const;

//: L'historique ne contient PAS que ce qui part d'ici : depuis l'audit
//: du 2026-08-19, tout courriel au locataire y atterrit, d'où qu'il
//: parte (fiche, bail, avis de renouvellement, relance…). Sans ces
//: libellés, ces lignes s'affichaient avec leur code brut
//: (« relance_loyer »). L'ordre suit celui du menu de filtre.
const TYPES_AUTOMATIQUES = [
  { value: "avis_renouvellement", label: "Avis de renouvellement" },
  { value: "document_signature", label: "Document à signer" },
  { value: "copie_signee", label: "Copie signée transmise" },
  { value: "document_courriel", label: "Document transmis" },
  { value: "relance_loyer", label: "Relance de loyer" },
] as const;

//: Tout ce qui peut apparaître dans l'historique = composable + envoyé
//: depuis ailleurs.
const TYPES_HISTORIQUE = [...TYPES, ...TYPES_AUTOMATIQUES] as ReadonlyArray<{
  value: string;
  label: string;
}>;

function typeLabel(t: string): string {
  return TYPES_HISTORIQUE.find((x) => x.value === t)?.label || t;
}

//: Miroir du plafond backend (`_MAX_DESTINATAIRES`) : on prévient AVANT
//: d'envoyer plutôt que de laisser le serveur refuser d'un bloc.
const MAX_DESTINATAIRES = 500;

//: Filtre sans accents ni casse (« tremblay » trouve « Tremblay »,
//: « elgin » trouve « Élgin »).
function normaliser(s: string | null | undefined): string {
  return (s || "")
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase();
}

function fmtDate(iso?: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("fr-CA", {
    dateStyle: "medium",
    timeStyle: "short"
  });
}

//: Suivi d'un envoi. Un courriel SIMPLE ne dit rien de plus que
//: « parti » : personne ne peut confirmer qu'il a été lu. Un envoi qui
//: portait un DOCUMENT, lui, sait quand le locataire a ouvert le lien
//: et quand il a signé — c'est ce qui tient devant un tribunal.
function SuiviCell({ r }: { r: AuditRow }) {
  if (r.statut !== "envoye") {
    return <span className="text-rose-300">Échec</span>;
  }
  if (!r.document_id) {
    return (
      <span className="text-white/35" title="Courriel simple — l'ouverture n'est pas traçable">
        Envoyé
      </span>
    );
  }
  if (r.document_signe_le) {
    return (
      <span
        className="text-emerald-300"
        title={`Signé par ${r.document_signe_par || "le locataire"} le ${fmtDate(r.document_signe_le)}`}
      >
        ✓ Signé {fmtDate(r.document_signe_le)}
      </span>
    );
  }
  if (r.document_ouvert_le) {
    return (
      <span className="text-sky-300">
        Ouvert {fmtDate(r.document_ouvert_le)}
        {r.document_signature_requise ? (
          <span className="block text-[10px] text-white/40">
            pas encore signé
          </span>
        ) : null}
      </span>
    );
  }
  return (
    <span className="text-amber-300/80" title="Le lien n'a pas encore été ouvert">
      Pas encore ouvert
    </span>
  );
}

export default function CommunicationsPage() {
  const { user: me } = useCurrentUser();
  // « De qui » : seuls les managers peuvent dévier des défauts — un
  // gestionnaire contractuel ne peut pas usurper l'expéditeur (le
  // backend le force aussi).
  const estManager = hasMinRole(me, "manager");
  const searchParams = useSearchParams();
  const [blocs, setBlocs] = useState<ImmeubleBloc[] | null>(null);
  const [reglages, setReglages] = useState<Reglages | null>(null);

  // À qui — la sélection est PAR LOCATAIRE (locSel) ; la case d'un
  // immeuble n'est qu'un raccourci tri-état sur ses locataires, et
  // l'envoi ne vise jamais « un immeuble » mais la liste des cochés.
  const [locSel, setLocSel] = useState<Map<number, Destinataire>>(new Map());
  // Filtre de la liste (nom, courriel, logement, immeuble).
  const [rechLoc, setRechLoc] = useState("");
  const [immOuverts, setImmOuverts] = useState<Set<number>>(new Set());
  // Gestion externe : jamais listée (c'est le gestionnaire tiers qui
  // parle à ses locataires) — le backend applique la même règle.

  // Quoi
  const [type, setType] = useState<string>("rappel_paiement");
  const [mois, setMois] = useState<string>(() => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  });
  const [accesDate, setAccesDate] = useState("");
  const [accesPlage, setAccesPlage] = useState("");
  const [accesMotif, setAccesMotif] = useState("");
  const [sujet, setSujet] = useState("");
  const [corps, setCorps] = useState(TEMPLATE_LIBRE);
  const corpsRef = useRef<HTMLTextAreaElement | null>(null);

  //: Insère {variable} là où est le curseur dans le texte du message.
  const insererVariable = (v: string) => {
    const tag = `{${v}}`;
    const el = corpsRef.current;
    if (!el) {
      setCorps((c) => c + tag);
      return;
    }
    const debut = el.selectionStart ?? el.value.length;
    const fin = el.selectionEnd ?? debut;
    setCorps(el.value.slice(0, debut) + tag + el.value.slice(fin));
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(debut + tag.length, debut + tag.length);
    });
  };

  // De qui — piloté par les PROFILS (l'expéditeur libre vit dans les
  // réglages, édité via la modale « Gérer les profils »).
  // "" = profil par défaut des réglages ; sinon label d'un profil choisi.
  const [profilSel, setProfilSel] = useState("");
  // Modale « Gérer les profils » (manager) + formulaire d'ajout.
  const [gestionOuverte, setGestionOuverte] = useState(false);
  const [nvLabel, setNvLabel] = useState("");
  const [nvEmail, setNvEmail] = useState("");
  const [nvNom, setNvNom] = useState("");
  const [nvReply, setNvReply] = useState("");
  const [savingReglages, setSavingReglages] = useState(false);
  const [reglagesMsg, setReglagesMsg] = useState<string | null>(null);

  const [sending, setSending] = useState(false);
  const [resultat, setResultat] = useState<EnvoiResultat | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Audit
  const [audit, setAudit] = useState<AuditRow[] | null>(null);
  const [fImmeuble, setFImmeuble] = useState("");
  const [fType, setFType] = useState("");
  const [fQ, setFQ] = useState("");
  const [detail, setDetail] = useState<AuditRow | null>(null);

  const loadAudit = useCallback(async () => {
    const p = new URLSearchParams();
    if (fImmeuble) p.set("immeuble_id", fImmeuble);
    if (fType) p.set("type", fType);
    if (fQ.trim()) p.set("q", fQ.trim());
    const r = await authedFetch(
      `/api/v1/immobilier/communications?${p.toString()}`
    );
    if (r.ok) setAudit(await r.json());
  }, [fImmeuble, fType, fQ]);

  useEffect(() => {
    void (async () => {
      const [rd, rr] = await Promise.all([
        authedFetch("/api/v1/immobilier/communications/destinataires"),
        authedFetch("/api/v1/immobilier/communications/reglages")
      ]);
      if (rd.ok) setBlocs(await rd.json());
      if (rr.ok) {
        const cfg = (await rr.json()) as Reglages;
        setReglages(cfg);
        // Pré-sélectionne le profil par défaut (retour Phil v4).
        if (cfg.profil_defaut) setProfilSel(cfg.profil_defaut);
      }
    })();
  }, []);

  // Filet : la gestion externe est TOUJOURS hors liste (décision Phil
  // 2026-08-19). Si une sélection en mémoire vise un locataire devenu
  // invisible, elle part avec lui — sinon on enverrait à des gens que
  // l'écran ne montre plus (le backend les filtrerait de toute façon).
  useEffect(() => {
    if (!blocs) return;
    const locVisibles = new Set(
      blocs.flatMap((b) => b.locataires.map((l) => l.locataire_id))
    );
    setLocSel((prev) => {
      const next = new Map(prev);
      for (const id of prev.keys())
        if (!locVisibles.has(id)) next.delete(id);
      return next;
    });
  }, [blocs]);

  useEffect(() => {
    void loadAudit();
  }, [loadAudit]);

  // « Écrire à ce locataire » depuis sa fiche : ?locataire_id=N →
  // pré-coché en chip dès que les destinataires sont chargés.
  useEffect(() => {
    const lid = Number(searchParams.get("locataire_id") || "");
    if (!lid || !blocs) return;
    setLocSel((prev) => {
      if (prev.has(lid)) return prev;
      for (const b of blocs) {
        const l = b.locataires.find((x) => x.locataire_id === lid);
        if (l) return new Map(prev).set(lid, l);
      }
      return prev;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blocs]);

  // Destinataires effectifs = les locataires cochés, dans l'ordre de la
  // liste (immeuble puis logement) pour que les puces suivent l'écran.
  const effectifs = useMemo(() => {
    const out: (Destinataire & { immeuble: string })[] = [];
    const vus = new Set<number>();
    for (const b of blocs || []) {
      for (const l of b.locataires) {
        if (!locSel.has(l.locataire_id)) continue;
        out.push({ ...l, immeuble: b.immeuble_name });
        vus.add(l.locataire_id);
      }
    }
    // Filet : un coché absent de la liste (le filet ci-dessus le purge
    // normalement) reste visible plutôt que de disparaître en silence.
    for (const [id, l] of locSel) {
      if (!vus.has(id)) out.push({ ...l, immeuble: "" });
    }
    return out;
  }, [blocs, locSel]);

  const sansEmail = effectifs.filter((l) => !l.email).length;
  const tropDeDestinataires = effectifs.length > MAX_DESTINATAIRES;

  // Filtre : réduit la liste aux locataires dont le nom, le courriel, le
  // numéro de logement OU l'immeuble contient le texte (accents ignorés).
  // Un immeuble qui matche par son nom garde tous ses locataires.
  const filtre = normaliser(rechLoc.trim());
  const blocsVisibles = useMemo(() => {
    if (!blocs) return [];
    if (!filtre) return blocs;
    const out: ImmeubleBloc[] = [];
    for (const b of blocs) {
      if (normaliser(b.immeuble_name).includes(filtre)) {
        out.push(b);
        continue;
      }
      const locataires = b.locataires.filter(
        (l) =>
          normaliser(l.nom).includes(filtre) ||
          normaliser(l.email).includes(filtre) ||
          normaliser(l.logement).includes(filtre)
      );
      if (locataires.length > 0) out.push({ ...b, locataires });
    }
    return out;
  }, [blocs, filtre]);

  // Locataires actuellement AFFICHÉS (filtre compris) — ce sur quoi
  // agissent « Tous les locataires » et les cases d'immeuble.
  const locVisibles = useMemo(
    () => blocsVisibles.flatMap((b) => b.locataires),
    [blocsVisibles]
  );
  const visiblesTousCoches =
    locVisibles.length > 0 &&
    locVisibles.every((l) => locSel.has(l.locataire_id));

  // « n cochés / m locataires » par immeuble, sur le bloc COMPLET
  // (filtre ou pas) — pilote la case tri-état et le compteur.
  const statParImm = useMemo(() => {
    const m = new Map<number, { n: number; m: number }>();
    for (const b of blocs || []) {
      m.set(b.immeuble_id, {
        n: b.locataires.filter((l) => locSel.has(l.locataire_id)).length,
        m: b.locataires.length
      });
    }
    return m;
  }, [blocs, locSel]);

  const toggleLoc = (l: Destinataire) => {
    setLocSel((prev) => {
      const next = new Map(prev);
      if (next.has(l.locataire_id)) next.delete(l.locataire_id);
      else next.set(l.locataire_id, l);
      return next;
    });
  };

  //: Coche ou décoche d'un coup une liste de locataires (case d'immeuble,
  //: « Tout / Aucun » d'un bloc, « Tous les locataires »).
  const cocherLocataires = (liste: Destinataire[], cocher: boolean) => {
    setLocSel((prev) => {
      const next = new Map(prev);
      for (const l of liste) {
        if (cocher) next.set(l.locataire_id, l);
        else next.delete(l.locataire_id);
      }
      return next;
    });
  };

  //: Case d'immeuble : agit sur les locataires AFFICHÉS du bloc (tous
  //: sans filtre, les seuls correspondants avec). Tous déjà cochés → on
  //: décoche ; sinon on coche le reste.
  const toggleImm = (b: ImmeubleBloc) => {
    const tousCoches =
      b.locataires.length > 0 &&
      b.locataires.every((l) => locSel.has(l.locataire_id));
    cocherLocataires(b.locataires, !tousCoches);
  };

  //: « Tous les locataires » suit ce que la liste montre (filtre
  //: compris) ; tous déjà cochés → on décoche.
  const toutCocher = () => {
    cocherLocataires(locVisibles, !visiblesTousCoches);
  };

  const profils = reglages?.profils || [];
  const profilDefaut = reglages?.profil_defaut || "";
  const profilActif = profils.find((p) => p.label === profilSel) || null;
  // Ce qui sera réellement utilisé comme expéditeur (affichage + validation) :
  // profil choisi > profil par défaut > défauts plats des réglages.
  const profilEffectif =
    profilActif ||
    profils.find((p) => p.label === profilDefaut) ||
    null;
  const effFromEmail = profilEffectif
    ? profilEffectif.from_email
    : reglages?.from_email || "";

  // PUT complet des réglages (profils + profil par défaut), en conservant
  // les défauts plats existants. Réservé aux managers côté serveur.
  const persistReglages = async (
    next: ProfilEnvoi[],
    defaut: string,
    okMsg: string
  ) => {
    if (!reglages) return;
    setSavingReglages(true);
    setReglagesMsg(null);
    try {
      const r = await authedFetch(
        "/api/v1/immobilier/communications/reglages",
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            from_email: reglages.from_email,
            from_name: reglages.from_name,
            reply_to: reglages.reply_to,
            profils: next,
            profil_defaut: defaut
          })
        }
      );
      if (r.status === 403) {
        setReglagesMsg("Réservé aux gestionnaires.");
        return;
      }
      if (!r.ok) throw new Error();
      const cfg = (await r.json()) as Reglages;
      setReglages(cfg);
      setReglagesMsg(okMsg);
    } catch {
      setReglagesMsg("Enregistrement impossible.");
    } finally {
      setSavingReglages(false);
    }
  };

  const ajouterProfil = async () => {
    const label = nvLabel.trim();
    if (!label) {
      setReglagesMsg("Donne un nom au profil (ex. « Kyle »).");
      return;
    }
    if (!nvEmail.trim()) {
      setReglagesMsg("L'adresse d'envoi (boîte Microsoft 365) est requise.");
      return;
    }
    const next = [
      ...profils.filter((p) => p.label !== label),
      {
        label,
        from_email: nvEmail.trim(),
        from_name: nvNom.trim(),
        reply_to: nvReply.trim()
      }
    ];
    // Le 1er profil créé devient automatiquement le défaut.
    const defaut = profils.length === 0 ? label : profilDefaut;
    await persistReglages(next, defaut, `Profil « ${label} » enregistré.`);
    setNvLabel("");
    setNvEmail("");
    setNvNom("");
    setNvReply("");
    setProfilSel(label);
  };

  const supprimerProfil = async (label: string) => {
    if (!window.confirm(`Supprimer le profil d'expéditeur « ${label} » ?`))
      return;
    const next = profils.filter((p) => p.label !== label);
    const defaut =
      profilDefaut === label ? next[0]?.label || "" : profilDefaut;
    await persistReglages(next, defaut, `Profil « ${label} » supprimé.`);
    if (profilSel === label) setProfilSel(defaut);
  };

  const definirDefaut = async (label: string) => {
    await persistReglages(profils, label, `« ${label} » est le défaut.`);
  };

  const envoyer = async () => {
    setErr(null);
    setResultat(null);
    const nb = effectifs.length - sansEmail;
    if (effectifs.length === 0) {
      setErr("Coche au moins un locataire (ou un immeuble entier).");
      return;
    }
    if (tropDeDestinataires) {
      setErr(
        `Trop de destinataires (${effectifs.length}) — maximum ${MAX_DESTINATAIRES} par envoi. Réduis la sélection ou envoie en plusieurs fois.`
      );
      return;
    }
    if (type === "libre" && (!sujet.trim() || !corps.trim())) {
      setErr("Message libre : remplis le sujet et le texte.");
      return;
    }
    if (type === "avis_acces" && !accesDate) {
      setErr("Avis d'accès : choisis la date de la visite.");
      return;
    }
    if (!effFromEmail.trim()) {
      setErr(
        estManager
          ? "Remplis la section « De qui » : l'adresse d'envoi est obligatoire."
          : "La section « De qui » n'est pas configurée — demande à ton gestionnaire d'enregistrer les défauts d'envoi ou choisis un profil."
      );
      return;
    }
    if (
      !window.confirm(
        `Envoyer « ${typeLabel(type)} » ?\n\n${nb} courriel${nb > 1 ? "s" : ""} individuel${nb > 1 ? "s" : ""} (un par locataire, personnalisé).${sansEmail ? `\n${sansEmail} locataire(s) sans courriel seront ignorés.` : ""}`
      )
    )
      return;
    setSending(true);
    try {
      const r = await authedFetch(
        "/api/v1/immobilier/communications/envoyer",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type,
            // Miroir exact de la liste : on n'envoie jamais « à un
            // immeuble », seulement aux locataires cochés (l'immeuble
            // n'est qu'un raccourci de sélection à l'écran).
            immeuble_ids: [],
            locataire_ids: effectifs.map((l) => l.locataire_id),
            sujet: type === "libre" ? sujet : undefined,
            corps: type === "libre" ? corps : undefined,
            mois: type === "rappel_paiement" ? `${mois}-01` : undefined,
            acces_date: type === "avis_acces" ? accesDate : undefined,
            acces_plage:
              type === "avis_acces" ? accesPlage || undefined : undefined,
            acces_motif:
              type === "avis_acces" ? accesMotif || undefined : undefined,
            // Profil sélectionné (défaut si non modifié) — le backend
            // retombe sur le profil par défaut si vide.
            profil: profilSel || undefined
          })
        }
      );
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        throw new Error((d && (d.detail || d.message)) || `Erreur ${r.status}`);
      }
      setResultat(d as EnvoiResultat);
      // Envoi parti → on remet la page à neuf (à qui, quoi) ; le
      // résumé vert reste affiché et le « De qui » est conservé.
      setLocSel(new Map());
      setRechLoc("");
      setType("rappel_paiement");
      setSujet("");
      setCorps(TEMPLATE_LIBRE);
      setAccesDate("");
      setAccesPlage("");
      setAccesMotif("");
      const auj = new Date();
      setMois(
        `${auj.getFullYear()}-${String(auj.getMonth() + 1).padStart(2, "0")}`
      );
      await loadAudit();
    } catch (e: any) {
      setErr(e?.message || "Envoi impossible");
    } finally {
      setSending(false);
    }
  };

  //: Un avis d'accès sans date, sans plage ou sans motif ne vaut
  //: rien devant le TAL — on bloque l'envoi plutôt que de laisser
  //: partir un avis inopposable (retour Phil 2026-08-19).
  const accesIncomplet =
    type === "avis_acces" &&
    (!accesDate.trim() || !accesPlage.trim() || !accesMotif.trim());

  return (
    <>
      <ImmobilierTopbar
        breadcrumbs={[
          { label: "Gestion immobilière", href: "/immobilier" },
          { label: "Communications" }
        ]}
      />

      <div className="space-y-5 p-4 pb-28 lg:p-6 lg:pb-28">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-white">
            <Mail className="h-6 w-6 text-accent-500" />
            Communications
          </h1>
          <p className="mt-1 max-w-2xl text-sm text-white/60">
            Avis sans signature et messages aux locataires — chaque envoi
            produit un courriel individuel par locataire et laisse une
            trace ici et sur sa fiche. Les documents à signer restent dans
            les sections Documents.
          </p>
        </div>

        {err ? (
          <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
            <AlertTriangle className="mr-1.5 inline h-3.5 w-3.5" />
            {err}
          </p>
        ) : null}
        {resultat ? (
          <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">
            <Check className="mr-1.5 inline h-3.5 w-3.5" />
            {resultat.envoyes} courriel{resultat.envoyes > 1 ? "s" : ""}{" "}
            envoyé{resultat.envoyes > 1 ? "s" : ""}.
            {resultat.ignores_payes.length > 0 && (
              <span className="block text-emerald-200/80">
                Déjà payé (sautés) : {resultat.ignores_payes.join(", ")}
              </span>
            )}
            {resultat.sans_email.length > 0 && (
              <span className="block text-amber-300">
                Sans courriel : {resultat.sans_email.join(", ")} — complète
                leur fiche pour les joindre.
              </span>
            )}
            {resultat.echecs.length > 0 && (
              <span className="block text-rose-300">
                Échecs : {resultat.echecs.join(" · ")}
              </span>
            )}
          </div>
        ) : null}

        {/* grid-cols-1 EXPLICITE : sans lui, la colonne unique mobile se
            dimensionne au contenu (auto) et déborde à droite sans pouvoir
            scroller (retour Phil v4). grid-cols-1 = minmax(0,1fr) borne la
            largeur ; les enfants tronquent alors correctement. */}
        <div className="grid min-w-0 grid-cols-1 gap-5 xl:grid-cols-2">
          {/* ── 1. À QUI ── */}
          <section className="rounded-2xl border border-brand-800 bg-brand-900 p-4 shadow-card sm:p-5">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h2 className="flex items-center gap-2 text-base font-bold text-white">
                <Users className="h-4 w-4 text-accent-500" /> À qui
              </h2>
              <div className="flex flex-wrap items-center gap-1.5">
                <button
                  className="btn-secondary btn-xs"
                  onClick={toutCocher}
                  disabled={locVisibles.length === 0}
                  title={
                    filtre
                      ? "Agit sur les locataires affichés par le filtre"
                      : "Coche tous les locataires de tous les immeubles"
                  }
                >
                  {visiblesTousCoches
                    ? "Tout décocher"
                    : filtre
                      ? "Tous les résultats"
                      : "Tous les locataires"}
                </button>
                <button
                  className="inline-flex items-center gap-1 rounded-lg border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs font-semibold text-rose-300 transition hover:bg-rose-500/20"
                  title="Sélectionner tous les locataires qui n'ont pas payé le mois courant au complet"
                  onClick={() => {
                    if (!blocs) return;
                    setLocSel((prev) => {
                      const next = new Map(prev);
                      for (const b of blocs) {
                        for (const l of b.locataires) {
                          if ((l.du_mois ?? 0) > 0.005)
                            next.set(l.locataire_id, l);
                        }
                      }
                      return next;
                    });
                  }}
                >
                  Retards du mois
                </button>
              </div>
            </div>

            {/* Gestion externe : jamais dans la liste — c'est leur
                gestionnaire qui écrit à ces locataires, et nous
                n'avons quasiment aucun de leurs courriels. */}
            <p className="mb-2 rounded-lg border border-brand-800 bg-brand-950/60 px-3 py-2 text-[10px] text-white/45">
              Les immeubles en gestion externe ne sont pas listés :
              c&apos;est leur gestionnaire qui communique avec ses
              locataires.
            </p>

            {blocs === null ? (
              <div className="flex items-center gap-2 py-6 text-xs text-white/50">
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Chargement…
              </div>
            ) : (
              <>
                {/* Filtre de la liste — réduit ET déplie les blocs qui
                    correspondent (nom, courriel, logement, immeuble). */}
                <div className="relative mb-2">
                  <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-white/40" />
                  <input
                    value={rechLoc}
                    onChange={(e) => setRechLoc(e.target.value)}
                    placeholder="Filtrer : nom, courriel, logement, immeuble…"
                    className="input w-full pl-8 pr-8 text-sm"
                  />
                  {rechLoc ? (
                    <button
                      type="button"
                      onClick={() => setRechLoc("")}
                      className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-white/40 hover:text-white"
                      title="Effacer le filtre"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  ) : null}
                </div>

                <div className="max-h-72 space-y-1.5 overflow-y-auto pr-1">
                  {blocsVisibles.map((b) => {
                    const stat = statParImm.get(b.immeuble_id) || {
                      n: 0,
                      m: 0
                    };
                    const tout = stat.m > 0 && stat.n === stat.m;
                    const partiel = stat.n > 0 && stat.n < stat.m;
                    // Un filtre actif déplie tout ce qui correspond.
                    const ouvert = !!filtre || immOuverts.has(b.immeuble_id);
                    return (
                      <div key={b.immeuble_id}>
                        <div className="flex items-center gap-2 rounded-lg border border-brand-800 bg-brand-950/60 px-3 py-2">
                          {/* Tri-état : cochée = tous ses locataires,
                              indéterminée = une partie (pas d'attribut
                              HTML pour ça → propriété DOM via ref). */}
                          <input
                            type="checkbox"
                            checked={tout}
                            ref={(el) => {
                              if (el) el.indeterminate = partiel;
                            }}
                            disabled={b.locataires.length === 0}
                            onChange={() => toggleImm(b)}
                            title={
                              tout
                                ? "Décocher tous les locataires de cet immeuble"
                                : "Cocher tous les locataires de cet immeuble"
                            }
                            className="h-4 w-4 shrink-0 accent-[var(--accent-500,#f59e0b)]"
                          />
                          <Building2 className="h-3.5 w-3.5 shrink-0 text-white/40" />
                          <span className="min-w-0 flex-1 truncate text-sm text-white">
                            {b.immeuble_name}
                          </span>
                          <span
                            className={`shrink-0 text-xs tabular-nums ${stat.n > 0 ? "text-accent-500" : "text-white/45"}`}
                            title={`${stat.n} locataire${stat.n > 1 ? "s" : ""} sélectionné${stat.n > 1 ? "s" : ""} sur ${stat.m}`}
                          >
                            {stat.n} / {stat.m}
                          </span>
                          <button
                            type="button"
                            className="rounded p-1 text-white/40 hover:text-white disabled:opacity-40"
                            title={
                              filtre
                                ? "Déplié par le filtre"
                                : "Voir les locataires"
                            }
                            disabled={!!filtre}
                            onClick={() =>
                              setImmOuverts((prev) => {
                                const n = new Set(prev);
                                if (n.has(b.immeuble_id))
                                  n.delete(b.immeuble_id);
                                else n.add(b.immeuble_id);
                                return n;
                              })
                            }
                          >
                            <ChevronDown
                              className={`h-3.5 w-3.5 transition ${ouvert ? "rotate-180" : ""}`}
                            />
                          </button>
                        </div>
                        {ouvert ? (
                          <div className="ml-8 mt-1 space-y-0.5">
                            <div className="flex flex-wrap items-center gap-1.5 pb-0.5 text-[10px] text-white/45">
                              <span className="tabular-nums">
                                {stat.n} / {stat.m} sélectionné
                                {stat.n > 1 ? "s" : ""}
                              </span>
                              {filtre && b.locataires.length < stat.m ? (
                                <span>
                                  ({b.locataires.length} affiché
                                  {b.locataires.length > 1 ? "s" : ""})
                                </span>
                              ) : null}
                              <span className="text-white/25">·</span>
                              <button
                                type="button"
                                className="font-semibold text-white/60 hover:text-white"
                                onClick={() =>
                                  cocherLocataires(b.locataires, true)
                                }
                              >
                                Tout
                              </button>
                              <span className="text-white/25">/</span>
                              <button
                                type="button"
                                className="font-semibold text-white/60 hover:text-white"
                                onClick={() =>
                                  cocherLocataires(b.locataires, false)
                                }
                              >
                                Aucun
                              </button>
                            </div>
                            {b.locataires.map((l) => (
                              <label
                                key={l.locataire_id}
                                className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 text-xs text-white/70 hover:bg-brand-950/60"
                              >
                                <input
                                  type="checkbox"
                                  checked={locSel.has(l.locataire_id)}
                                  onChange={() => toggleLoc(l)}
                                  className="h-3.5 w-3.5 shrink-0 accent-[var(--accent-500,#f59e0b)]"
                                />
                                <span className="min-w-0 truncate">
                                  {l.logement ? `${l.logement} · ` : ""}
                                  {l.nom}
                                </span>
                                {(l.du_mois ?? 0) > 0.005 ? (
                                  <span
                                    className="shrink-0 text-[10px] font-semibold text-rose-300"
                                    title="N'a pas payé le mois courant au complet"
                                  >
                                    retard
                                  </span>
                                ) : null}
                                {l.email ? (
                                  <span className="ml-auto hidden min-w-0 max-w-[45%] truncate text-white/35 sm:inline">
                                    {l.email}
                                  </span>
                                ) : (
                                  <span className="badge badge-amber ml-auto shrink-0">
                                    sans courriel
                                  </span>
                                )}
                              </label>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                  {blocs.length === 0 && (
                    <p className="py-4 text-center text-sm text-white/45">
                      Aucun bail actif — crée des baux d&apos;abord.
                    </p>
                  )}
                  {blocs.length > 0 && blocsVisibles.length === 0 && (
                    <p className="py-4 text-center text-sm text-white/45">
                      Aucun locataire ne correspond à « {rechLoc.trim()} ».
                    </p>
                  )}
                </div>

                {/* Puces récapitulatives = exactement les destinataires
                    effectifs (même ordre que la liste). */}
                {effectifs.length > 0 && (
                  <div className="mt-2 flex max-h-24 flex-wrap gap-1.5 overflow-y-auto pr-1">
                    {effectifs.map((l) => (
                      <span
                        key={l.locataire_id}
                        className={`badge inline-flex items-center gap-1 ${l.email ? "badge-neutral" : "badge-amber"}`}
                        title={[l.immeuble, l.logement, l.email || "sans courriel"]
                          .filter(Boolean)
                          .join(" · ")}
                      >
                        {l.nom}
                        <button
                          type="button"
                          onClick={() =>
                            setLocSel((prev) => {
                              const n = new Map(prev);
                              n.delete(l.locataire_id);
                              return n;
                            })
                          }
                          className="text-white/50 hover:text-rose-400"
                          title="Retirer de l'envoi"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </span>
                    ))}
                  </div>
                )}

                <div className="mt-3 flex items-center justify-between gap-2 border-t border-brand-800 pt-3">
                  <p className="text-sm text-white/60">
                    <strong className="text-white">{effectifs.length}</strong>{" "}
                    destinataire{effectifs.length > 1 ? "s" : ""}
                    {sansEmail > 0 && (
                      <span className="text-amber-300">
                        {" "}
                        · {sansEmail} sans courriel (ignoré
                        {sansEmail > 1 ? "s" : ""})
                      </span>
                    )}
                    {tropDeDestinataires && (
                      <span className="text-rose-300">
                        {" "}
                        · maximum {MAX_DESTINATAIRES} par envoi
                      </span>
                    )}
                  </p>
                  {locSel.size > 0 ? (
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 text-xs font-semibold text-white/50 transition hover:text-rose-300"
                      onClick={() => setLocSel(new Map())}
                      title="Désélectionner tous les locataires"
                    >
                      <X className="h-3.5 w-3.5" />
                      Tout effacer
                    </button>
                  ) : null}
                </div>
              </>
            )}
          </section>

          {/* ── 2. QUOI ── */}
          <section className="rounded-2xl border border-brand-800 bg-brand-900 p-4 shadow-card sm:p-5">
            <h2 className="mb-3 flex items-center gap-2 text-base font-bold text-white">
              <Mail className="h-4 w-4 text-accent-500" /> Quoi
            </h2>
            <p className="mb-2 text-[11px] font-medium uppercase tracking-wide text-white/45">
              Avis (modèles courriel — texte modifiable dans Modèles de
              documents)
            </p>
            <div className="space-y-1.5">
              {TYPES.map((t) => (
                <label
                  key={t.value}
                  className={`block cursor-pointer rounded-lg border px-3 py-2 transition ${
                    type === t.value
                      ? "border-accent-500/60 bg-accent-500/5"
                      : "border-brand-800 bg-brand-950/60 hover:border-brand-700"
                  } ${t.value === "libre" ? "mt-4" : ""}`}
                >
                  {t.value === "libre" && (
                    <span className="mb-1 -mt-0.5 block text-[11px] font-medium uppercase tracking-wide text-white/45">
                      Ou message personnalisé
                    </span>
                  )}
                  <span className="flex items-center gap-2">
                    <input
                      type="radio"
                      name="type"
                      checked={type === t.value}
                      onChange={() => setType(t.value)}
                      className="h-3.5 w-3.5 accent-[var(--accent-500,#f59e0b)]"
                    />
                    <span className="text-sm font-medium text-white">
                      {t.label}
                    </span>
                  </span>
                  <span className="mt-0.5 block pl-5 text-xs text-white/50">
                    {t.desc}
                  </span>
                </label>
              ))}
            </div>

            {type === "rappel_paiement" && (
              <div className="mt-3">
                <label className="text-xs font-medium text-white/60">
                  Mois réclamé
                </label>
                <input
                  type="month"
                  value={mois}
                  onChange={(e) => setMois(e.target.value)}
                  className="input mt-1 block"
                />
              </div>
            )}
            {type === "avis_acces" && (
              <div className="mt-3 grid gap-2 sm:grid-cols-3">
                <div>
                  <label className="text-xs font-medium text-white/60">
                    Date *
                  </label>
                  <input
                    type="date"
                    value={accesDate}
                    onChange={(e) => setAccesDate(e.target.value)}
                    className="input mt-1 w-full"
                  />
                </div>
                <div>
                  <label className="text-xs font-medium text-white/60">
                    Plage horaire *
                  </label>
                  <input
                    value={accesPlage}
                    onChange={(e) => setAccesPlage(e.target.value)}
                    placeholder="entre 9 h et 12 h"
                    className="input mt-1 w-full"
                  />
                </div>
                <div>
                  <label className="text-xs font-medium text-white/60">
                    Motif *
                  </label>
                  <input
                    value={accesMotif}
                    onChange={(e) => setAccesMotif(e.target.value)}
                    placeholder="inspection des détecteurs"
                    className="input mt-1 w-full"
                  />
                </div>
              </div>
            )}
            {type === "libre" && (
              <div className="mt-3 space-y-2">
                <input
                  value={sujet}
                  onChange={(e) => setSujet(e.target.value)}
                  placeholder="Sujet du courriel"
                  className="input w-full"
                />
                <textarea
                  ref={corpsRef}
                  value={corps}
                  onChange={(e) => setCorps(e.target.value)}
                  rows={6}
                  placeholder={
                    "Bonjour {locataire},\n\n…\n\n{locateur}"
                  }
                  className="w-full rounded-lg border border-brand-800 bg-brand-950 px-3 py-2 text-sm text-white outline-none transition focus:border-accent-500"
                />
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-xs text-white/45">Insérer :</span>
                  {["locataire", "adresse", "logement", "locateur"].map(
                    (v) => (
                      <button
                        key={v}
                        type="button"
                        onClick={() => insererVariable(v)}
                        className="rounded-md border border-brand-800 bg-brand-950 px-2 py-0.5 font-mono text-[11px] text-white/70 transition hover:border-accent-500/60 hover:text-white"
                        title="Insérer à la position du curseur"
                      >
                        {`{${v}}`}
                      </button>
                    )
                  )}
                </div>
                <p className="text-xs text-white/45">
                  Chaque variable est remplacée pour chaque locataire.{" "}
                  <code className="text-white/60">{"{locateur}"}</code> ={" "}
                  le nom du « De qui » (ex. Kyle Brown - Gestion
                  locative) — c&apos;est la signature de base.
                </p>
              </div>
            )}
          </section>
        </div>

        {/* ── 3. DE QUI + ENVOYER ── */}
        <section className="rounded-2xl border border-brand-800 bg-brand-900 p-4 shadow-card sm:p-5">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="flex items-center gap-2 text-base font-bold text-white">
              <Settings className="h-4 w-4 text-accent-500" /> De qui
            </h2>
            {estManager ? (
              <button
                type="button"
                className="btn-secondary btn-xs"
                onClick={() => setGestionOuverte(true)}
                title="Créer, supprimer et choisir le profil d'expéditeur par défaut"
              >
                <Settings className="h-3.5 w-3.5" /> Gérer les profils
              </button>
            ) : null}
          </div>

          {profils.length > 0 ? (
            <>
              {/* Sélecteur de profil — le profil ★ est le défaut. Tout le
                  monde peut choisir un autre profil pour cet envoi. */}
              <div className="flex flex-wrap items-center gap-2">
                {profils.map((pr) => {
                  const sel =
                    profilSel === pr.label ||
                    (profilSel === "" && pr.label === profilDefaut);
                  return (
                    <button
                      key={pr.label}
                      type="button"
                      onClick={() => setProfilSel(pr.label)}
                      className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-semibold transition ${
                        sel
                          ? "bg-accent-500 text-brand-950"
                          : "border border-white/10 bg-brand-950 text-white/70 hover:text-white"
                      }`}
                      title={`${pr.from_name || pr.from_email} <${pr.from_email}>`}
                    >
                      {pr.label === profilDefaut ? (
                        <Star className="h-3 w-3" />
                      ) : null}
                      {pr.label}
                    </button>
                  );
                })}
              </div>
              {profilEffectif ? (
                <p className="mt-2 text-xs text-white/55">
                  Envoie depuis{" "}
                  <span className="font-medium text-white/80">
                    {profilEffectif.from_name || profilEffectif.from_email}
                  </span>{" "}
                  &lt;{profilEffectif.from_email}&gt;
                  {profilEffectif.reply_to ? (
                    <> · réponses → {profilEffectif.reply_to}</>
                  ) : null}
                </p>
              ) : null}
            </>
          ) : reglages?.from_email ? (
            <p className="text-sm text-white/70">
              Expéditeur :{" "}
              <span className="font-medium text-white">
                {reglages.from_name || reglages.from_email}
              </span>{" "}
              &lt;{reglages.from_email}&gt;
              {estManager ? (
                <span className="mt-1 block text-xs text-white/45">
                  Plusieurs gestionnaires ? Ouvre « Gérer les profils » pour
                  créer des expéditeurs sélectionnables par toute l&apos;équipe.
                </span>
              ) : null}
            </p>
          ) : (
            <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
              Aucun expéditeur configuré.{" "}
              {estManager
                ? "Ouvre « Gérer les profils » pour en créer un."
                : "Demande à ton gestionnaire d'en configurer un."}
            </p>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              className="btn-accent btn-sm"
              disabled={
                sending || effectifs.length === 0 || accesIncomplet
              }
              title={
                accesIncomplet
                  ? "Avis d'accès : la date, la plage horaire et le motif sont obligatoires (art. 1932-1933 C.c.Q.)"
                  : undefined
              }
              onClick={() => void envoyer()}
            >
              {sending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Send className="h-4 w-4" />
              )}
              Envoyer ({Math.max(0, effectifs.length - sansEmail)})
            </button>
            {reglagesMsg && (
              <span className="text-xs text-white/60">{reglagesMsg}</span>
            )}
          </div>
        </section>

        {/* Modale « Gérer les profils » (manager) — création, suppression
            et choix du profil par défaut. */}
        {gestionOuverte ? (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
            onClick={() => setGestionOuverte(false)}
          >
            <div
              className="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-brand-800 bg-brand-900 p-4 shadow-card sm:p-5"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="mb-3 flex items-center justify-between">
                <h3 className="flex items-center gap-2 text-base font-bold text-white">
                  <Settings className="h-4 w-4 text-accent-500" /> Profils
                  d&apos;expéditeur
                </h3>
                <button
                  onClick={() => setGestionOuverte(false)}
                  className="text-white/50 transition hover:text-white"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <p className="mb-3 text-xs text-white/50">
                Chaque profil = un expéditeur (boîte Microsoft 365 + nom
                affiché + adresse de réponse). Tout le monde choisit parmi ces
                profils ; le profil ★ est proposé par défaut.
              </p>

              <div className="space-y-1.5">
                {profils.length === 0 ? (
                  <p className="text-sm text-white/45">
                    Aucun profil pour l&apos;instant — crée le premier
                    ci-dessous.
                  </p>
                ) : (
                  profils.map((pr) => (
                    <div
                      key={pr.label}
                      className="flex items-center gap-2 rounded-lg border border-brand-800 bg-brand-950/60 px-3 py-2"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5 text-sm font-semibold text-white">
                          {pr.label === profilDefaut ? (
                            <Star className="h-3.5 w-3.5 text-accent-500" />
                          ) : null}
                          {pr.label}
                        </div>
                        <div className="truncate text-xs text-white/50">
                          {pr.from_name ? `${pr.from_name} · ` : ""}
                          {pr.from_email}
                          {pr.reply_to ? ` · ↩ ${pr.reply_to}` : ""}
                        </div>
                      </div>
                      {pr.label === profilDefaut ? (
                        <span className="badge badge-emerald shrink-0">
                          Défaut
                        </span>
                      ) : (
                        <button
                          onClick={() => void definirDefaut(pr.label)}
                          disabled={savingReglages}
                          className="btn-secondary btn-xs shrink-0"
                          title="Proposer ce profil par défaut à toute l'équipe"
                        >
                          Par défaut
                        </button>
                      )}
                      <button
                        onClick={() => void supprimerProfil(pr.label)}
                        disabled={savingReglages}
                        className="shrink-0 rounded p-1 text-white/40 transition hover:text-rose-300"
                        title={`Supprimer « ${pr.label} »`}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ))
                )}
              </div>

              <div className="mt-4 space-y-2 border-t border-brand-800 pt-3">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-white/45">
                  Nouveau profil
                </p>
                <input
                  value={nvLabel}
                  onChange={(e) => setNvLabel(e.target.value)}
                  placeholder="Nom du profil (ex. Kyle)"
                  className="input w-full text-sm"
                />
                <input
                  value={nvEmail}
                  onChange={(e) => setNvEmail(e.target.value)}
                  placeholder="Adresse d'envoi — boîte Microsoft 365 (ex. info@immohorizon.com)"
                  className="input w-full text-sm"
                />
                <input
                  value={nvNom}
                  onChange={(e) => setNvNom(e.target.value)}
                  placeholder="Nom affiché (ex. Kyle — Gestion Horizon)"
                  className="input w-full text-sm"
                />
                <input
                  value={nvReply}
                  onChange={(e) => setNvReply(e.target.value)}
                  placeholder="Répondre à — peut être externe (ex. kyle.gestion@gmail.com)"
                  className="input w-full text-sm"
                />
                <p className="text-[11px] text-white/40">
                  L&apos;adresse d&apos;envoi doit être une boîte de votre
                  Microsoft 365. Pour un gestionnaire externe, mets son adresse
                  dans « Répondre à » — les réponses lui iront directement.
                </p>
                <div className="flex items-center gap-2">
                  <button
                    className="btn-accent btn-sm"
                    disabled={savingReglages}
                    onClick={() => void ajouterProfil()}
                  >
                    {savingReglages ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Plus className="h-3.5 w-3.5" />
                    )}
                    Ajouter le profil
                  </button>
                  {reglagesMsg && (
                    <span className="text-xs text-white/60">
                      {reglagesMsg}
                    </span>
                  )}
                </div>
              </div>
            </div>
          </div>
        ) : null}

        {/* ── 4. AUDIT ── */}
        <section className="rounded-2xl border border-brand-800 bg-brand-900 p-4 shadow-card sm:p-5">
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <h2 className="flex items-center gap-2 text-base font-bold text-white">
              <Filter className="h-4 w-4 text-accent-500" /> Envois passés
            </h2>
            <select
              value={fImmeuble}
              onChange={(e) => setFImmeuble(e.target.value)}
              className="input py-1.5 text-sm"
            >
              <option value="">Tous les immeubles</option>
              {(blocs || []).map((b) => (
                <option key={b.immeuble_id} value={String(b.immeuble_id)}>
                  {b.immeuble_name}
                </option>
              ))}
            </select>
            <select
              value={fType}
              onChange={(e) => setFType(e.target.value)}
              className="input py-1.5 text-sm"
            >
              <option value="">Tous les types</option>
              {TYPES_HISTORIQUE.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-white/40" />
              <input
                value={fQ}
                onChange={(e) => setFQ(e.target.value)}
                placeholder="Locataire, sujet, courriel…"
                className="input py-1.5 pl-8 text-sm"
              />
            </div>
          </div>

          {audit === null ? (
            <div className="flex items-center gap-2 py-6 text-xs text-white/50">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Chargement…
            </div>
          ) : audit.length === 0 ? (
            <p className="rounded-xl border border-dashed border-brand-800 px-5 py-6 text-center text-sm text-white/45">
              Aucun envoi pour l&apos;instant.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[820px] text-sm">
                <thead>
                  <tr className="border-b border-brand-800 text-left text-xs uppercase tracking-wide text-white/50">
                    <th className="px-3 py-2">Quand</th>
                    <th className="px-3 py-2">Type</th>
                    <th className="px-3 py-2">Locataire</th>
                    <th className="px-3 py-2">Immeuble</th>
                    <th className="px-3 py-2">Sujet</th>
                    <th className="px-3 py-2">Suivi</th>
                    <th className="px-3 py-2">Par</th>
                    <th className="px-3 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {audit.map((r) => (
                    <tr
                      key={r.id}
                      className="border-b border-brand-800/60 last:border-b-0"
                    >
                      <td className="whitespace-nowrap px-3 py-2 text-white/60">
                        {fmtDate(r.created_at)}
                      </td>
                      <td className="px-3 py-2">
                        <span className="badge badge-neutral">
                          {typeLabel(r.type)}
                        </span>
                        {r.statut !== "envoye" && (
                          <span className="badge badge-rose ml-1">échec</span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-white">
                        {r.locataire_nom || "—"}
                        <span className="block text-xs text-white/45">
                          {r.destinataire_email}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-white/70">
                        {r.immeuble_nom || "—"}
                      </td>
                      <td className="max-w-[260px] truncate px-3 py-2 text-white/70">
                        {r.sujet}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-xs">
                        <SuiviCell r={r} />
                      </td>
                      <td
                        className="px-3 py-2 text-white/50"
                        title={r.created_by_email || undefined}
                      >
                        {r.created_by_nom || r.created_by_email || "—"}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <button
                          className="rounded-lg p-1.5 text-white/40 transition hover:bg-brand-950 hover:text-white"
                          title="Voir le courriel"
                          onClick={() => setDetail(r)}
                        >
                          <Eye className="h-4 w-4" />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>

      {detail ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setDetail(null)}
        >
          <div
            className="max-h-[80vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-brand-800 bg-brand-900 p-4 shadow-card sm:p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-start justify-between gap-3">
              <div>
                <h3 className="text-base font-bold text-white">
                  {detail.sujet}
                </h3>
                <p className="mt-0.5 text-xs text-white/50">
                  À {detail.locataire_nom || "—"} &lt;
                  {detail.destinataire_email}&gt; · {fmtDate(detail.created_at)}
                </p>
                <p className="text-xs text-white/50">
                  De {detail.from_name || ""}{" "}
                  {detail.from_email ? `<${detail.from_email}>` : "(défaut)"}
                  {detail.reply_to
                    ? ` · répondre à ${detail.reply_to}`
                    : ""}
                </p>
              </div>
              <button
                className="rounded-lg p-1.5 text-white/40 hover:text-white"
                onClick={() => setDetail(null)}
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="whitespace-pre-wrap rounded-lg border border-brand-800 bg-brand-950/60 px-4 py-3 text-sm leading-relaxed text-white/80">
              {detail.corps}
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
