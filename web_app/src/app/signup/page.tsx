import SignupForm from "@/components/SignupForm";

export default async function SignupPage({
  searchParams,
}: {
  searchParams: Promise<{ station_id?: string }>;
}) {
  const { station_id } = await searchParams;

  return (
    <main className="flex flex-1 flex-col items-center gap-6 p-8 sm:p-16">
      <div className="text-center">
        <h1 className="text-3xl font-semibold tracking-tight">Get Alerts</h1>
        <p className="mt-2 max-w-md text-sm text-zinc-600 dark:text-zinc-400">
          Know before you go. We&apos;ll notify you when a bike is predicted to
          be available at your station.
        </p>
      </div>
      <SignupForm initialStationId={station_id ?? ""} />
    </main>
  );
}
