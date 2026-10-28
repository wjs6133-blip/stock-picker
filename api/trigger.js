export default async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  const token = process.env.GH_TOKEN;
  if (!token) return res.status(500).json({ error: "GH_TOKEN 미설정" });

  const r = await fetch(
    "https://api.github.com/repos/wjs6133-blip/stock-picker/actions/workflows/daily-analysis.yml/dispatches",
    {
      method: "POST",
      headers: { Authorization: `token ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ ref: "main" }),
    }
  );
  if (r.status === 204) return res.json({ ok: true });
  return res.status(500).json({ error: await r.text() });
}
