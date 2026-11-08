import MissionList from "@/components/MissionList";
import NewMissionForm from "@/components/NewMissionForm";

export default function HomePage() {
  return (
    <div className="stack">
      <NewMissionForm />
      <MissionList />
    </div>
  );
}
