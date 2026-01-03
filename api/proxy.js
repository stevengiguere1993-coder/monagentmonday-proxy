export default async function handler(req, res) {
  try {
    const response = await fetch("https://mondayagent-bridge.onrender.com/job", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": "Police-138"
      },
      body: JSON.stringify(req.body)
    });

    const data = await response.json();
    res.status(response.status).json(data);
  } catch (err) {
    console.error("Proxy error:", err);
    res.status(500).json({ ok: false, error: err.message });
  }
}
