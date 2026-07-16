import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Dashboard | Citi Bike Predictions",
  description: "Historical ridership and model performance analytics.",
};

const TABLEAU_URL =
  "https://public.tableau.com/views/citibike_dashboard_v1/Dashboard1?:showVizHome=no&:embed=true";

export default function DashboardPage() {
  return (
    <div className="flex flex-1 flex-col">
      <iframe
        src={TABLEAU_URL}
        title="Citi Bike analytics dashboard"
        className="w-full flex-1 border-0"
        allowFullScreen
      />
    </div>
  );
}
