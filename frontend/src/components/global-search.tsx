"use client";

import { useEffect, useRef, useState } from "react";
import { Search, Loader2 } from "lucide-react";

import { authedFetch } from "@/lib/auth";

type Hit = {
  kind:
    | "client"
    | "prospect"
    | "soumission"
    | "facture"
    | "project"
    | "bon"
    | "employe"
    | "locataire";
  id: number;
  title: string;
  subtitle: string | null;
  href: string;
};

const KIND_LABEL: Record<Hit["kind"], string> = {
  client: "Client",
  prospect: "Prospect",
  soumission: "Soumission",
  facture: "Facture",
  project: "Projet",
  bon: "Bon de travail",
  employe: "Employé",
  locataire: "Locataire"
};

const KIND_ORDER: Hit["kind"][] = [
  "client",
  "prospect",
  "soumission",
  "facture",
  "project",
  "bon",
  "employe",
  "locataire"
];

export function GlobalSearch() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  // Close on outside click
  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  // Debounced search
  useEffect(() => {
    const needle = q.trim();
    if (needle.length < 2) {
      setHits([]);
      return;
    }
    // Garde anti-race : si `q` change (donc l'effet est re-exécuté) avant
    // que le fetch en vol ne réponde, on ignore sa réponse. Sans ça, une
    // requête lente pour un ancien terme pouvait écraser les résultats à
    // jour d'un terme plus récent (réponses hors-ordre).
    let ignore = false;
    const t = setTimeout(async () => {
      setLoading(true);
      try {
        const res = await authedFetch(
          `/api/v1/search?q=${encodeURIComponent(needle)}&limit=5`
        );
        if (!res.ok) return;
        const data = (await res.json()) as Hit[];
        if (!ignore) {
          setHits(data);
          setActive(0);
        }
      } finally {
        if (!ignore) setLoading(false);
      }
    }, 180);
    return () => {
      ignore = true;
      clearTimeout(t);
    };
  }, [q]);

  function onKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(hits.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      const h = hits[active];
      if (h) {
        window.location.href = `/fr${h.href}`;
      }
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  // Group by kind in fixed order
  const grouped = KIND_ORDER.map((k) => ({
    kind: k,
    items: hits.filter((h) => h.kind === k)
  })).filter((g) => g.items.length > 0);

  return (
    <div ref={wrapRef} className="relative hidden min-w-[220px] md:block">
      <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/40" />
      <input
        type="search"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => setOpen(true)}
        onKeyDown={onKey}
        placeholder="Rechercher client, prospect, locataire, soumission…"
        className="w-full rounded-lg border border-brand-800 bg-brand-900 py-2 pl-9 pr-9 text-sm text-white placeholder:text-white/40 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/40"
      />
      {loading ? (
        <Loader2 className="absolute right-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 animate-spin text-white/40" />
      ) : null}

      {open && q.trim().length >= 2 ? (
        <div className="absolute left-0 right-0 top-full z-40 mt-1 max-h-[70vh] overflow-y-auto rounded-xl border border-brand-800 bg-brand-950 shadow-xl">
          {grouped.length === 0 && !loading ? (
            <p className="px-4 py-6 text-center text-xs text-white/50">
              Aucun résultat pour « {q} »
            </p>
          ) : (
            grouped.map((g) => (
              <div key={g.kind}>
                <p className="border-b border-brand-800/60 bg-brand-900 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-accent-500">
                  {KIND_LABEL[g.kind]} ({g.items.length})
                </p>
                {g.items.map((h) => {
                  const idx = hits.indexOf(h);
                  const isActive = idx === active;
                  return (
                    <a
                      key={`${h.kind}-${h.id}`}
                      href={`/fr${h.href}`}
                      onMouseEnter={() => setActive(idx)}
                      className={`block border-b border-brand-800/40 px-3 py-2 text-sm ${
                        isActive
                          ? "bg-accent-500/10 text-white"
                          : "text-white/80 hover:bg-brand-900"
                      }`}
                    >
                      <p className="truncate font-medium">{h.title}</p>
                      {h.subtitle ? (
                        <p className="truncate text-[11px] text-white/50">
                          {h.subtitle}
                        </p>
                      ) : null}
                    </a>
                  );
                })}
              </div>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}
