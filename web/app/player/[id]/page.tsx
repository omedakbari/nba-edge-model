import Link from "next/link";
import { notFound } from "next/navigation";
import PlayerView from "@/components/PlayerView";
import { findPlayer, loadSlate } from "@/lib/slate";

export function generateStaticParams() {
  return loadSlate().players.map((p) => ({ id: String(p.player_id) }));
}

export default async function PlayerPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const slate = loadSlate();
  const player = findPlayer(slate, Number(id));
  if (!player) notFound();
  return (
    <main>
      <Link href="/" className="small">‹ Tonight</Link>
      <PlayerView player={player} params={slate.params} asOf={slate.generated_at} />
    </main>
  );
}
