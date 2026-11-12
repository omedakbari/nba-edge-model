import Link from "next/link";
import { loadSlate } from "@/lib/slate";

export default function Home() {
  const slate = loadSlate();
  return (
    <main>
      <h1>Tonight</h1>
      <p className="muted small">Sample data (synthetic) as of {new Date(slate.generated_at).toLocaleString("en-US", { timeZone: "America/New_York" })} ET</p>
      <div className="banner">Layout preview: these players and prices are made up. The real slate comes from the database after the Phase 2 gate.</div>
      <ul className="list">
        {slate.players.map((p) => (
          <li key={p.player_id} className="card">
            <Link href={`/player/${p.player_id}`}>
              <span>
                <strong>{p.name}</strong>{" "}
                <span className="muted small">{p.team} {p.is_home ? "vs" : "@"} {p.opp}</span>
              </span>
              {p.injury ? <span className="small">{p.injury.code}</span> : <span className="muted small">›</span>}
            </Link>
          </li>
        ))}
      </ul>
    </main>
  );
}
