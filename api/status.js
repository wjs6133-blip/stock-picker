export default async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  const token = process.env.GH_TOKEN;
  if (!token) return res.status(500).json({ error: "GH_TOKEN 미설정" });

  const r = await fetch(
    "https://api.github.com/repos/wjs6133-blip/stock-picker/actions/runs?per_page=1",
    { headers: { Authorization: `token ${token}` } }
  );
  const data = await r.json();
  const run = data.workflow_runs?.[0];
  if (!run) return res.json({ status: "unknown" });

  return res.json({
    status: run.status,          // queued | in_progress | completed
    conclusion: run.conclusion,  // success | failure | null
    created_at: run.created_at,
  });
}
