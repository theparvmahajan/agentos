import { notFound } from "next/navigation";
import MissionView from "@/components/MissionView";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function MissionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID.test(id)) notFound(); // never open a connection for something that cannot be a mission
  return <MissionView key={id} id={id} />;
}
