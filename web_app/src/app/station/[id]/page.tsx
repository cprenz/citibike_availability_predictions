import StationDetail from "@/components/StationDetail";

type StationPageProps = {
  params: Promise<{ id: string }>;
};

export default async function StationPage({ params }: StationPageProps) {
  const { id } = await params;

  return <StationDetail stationId={id} />;
}
