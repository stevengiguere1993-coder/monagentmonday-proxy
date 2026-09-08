// Portes de fonctionnalités en construction — testées sur la version
// dev (staging) AVANT d'apparaître en prod, même si le code est promu.
//
// Chantier « stratégies d'acquisition » du calculateur Prospection
// (Phil, 2026-08-31) : tout se construit et se teste sur
// h2-0-web-dev.onrender.com ; au GO de Phil, on retourne `true` sans
// condition (ou on retire la porte).

function surStaging(): boolean {
  if (typeof window === "undefined") return false;
  const h = window.location.hostname;
  return (
    h.includes("h2-0-web-dev") ||
    h === "localhost" ||
    h === "127.0.0.1"
  );
}

export function calculateurStrategiesActif(): boolean {
  return surStaging();
}

// Chantier « chacun son IA » (Phil, 2026-09-01) : connexion IA
// personnelle + brief quotidien — testé sur staging avant le GO.
export function iaPersonnelleActive(): boolean {
  return surStaging();
}
