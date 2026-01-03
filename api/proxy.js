export default async function handler(req, res) {
  try {
    // 🔹 Vérifie que le body est présent
    const payload = req.body;
    if (!payload) {
      return res.status(400).json({ ok: false, error: "Missing JSON body" });
    }

    // 🔹 Envoi vers ton backend Render
    const response = await fetch("https://mondayagent-bridge.onrender.com/job", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": "Police-138"
      },
      body: JSON.stringify(payload)
    });

    // 🔹 Gestion des réponses Render
    if (!response.ok) {
      const text = await response.text();
      console.error("❌ Erreur Render:", response.status, text);
      return res
        .status(response.status)
        .json({ ok: false, status: response.status, body: text });
    }

    // 🔹 Succès → renvoyer le JSON complet à l’appelant
    const data = await response.json();
    res.status(200).json(data);

  } catch (err) {
    // 🔹 Gestion globale des exceptions
    console.error("⚠️ Exception proxy → Render:", err);
    res.status(500).json({ ok: false, error: err.message });
  }
}
