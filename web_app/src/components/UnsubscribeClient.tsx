"use client";

import { useState } from "react";

export default function UnsubscribeClient({ id }: { id: string | null }) {
  const [status, setStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState("");

  if (!id) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center p-8 text-center">
        <p className="text-gray-600 dark:text-gray-400 mb-4">Invalid unsubscribe link.</p>
        <a href="/" className="text-blue-600 hover:underline text-sm">Back to the map</a>
      </div>
    );
  }

  if (status === "done") {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center p-8 text-center">
        <div className="text-4xl mb-4">&#10003;</div>
        <h1 className="text-xl font-bold mb-2">You've been unsubscribed</h1>
        <p className="text-gray-600 dark:text-gray-400 mb-6 text-sm">
          You won't receive any more Citi Bike availability alerts.
        </p>
        <a href="/" className="text-blue-600 hover:underline text-sm">Back to the map</a>
      </div>
    );
  }

  async function handleUnsubscribe() {
    setStatus("loading");
    setErrorMsg("");
    try {
      const res = await fetch("/api/unsubscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: parseInt(id!) }),
      });
      const data = await res.json();
      if (res.ok) {
        setStatus("done");
      } else {
        setStatus("error");
        setErrorMsg(data.error ?? "Something went wrong. Try again.");
      }
    } catch {
      setStatus("error");
      setErrorMsg("Network error. Try again.");
    }
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center p-8 text-center">
      <h1 className="text-xl font-bold mb-2">Unsubscribe from alerts</h1>
      <p className="text-gray-600 dark:text-gray-400 mb-6 text-sm max-w-xs">
        Click below to stop receiving Citi Bike availability alerts from this subscription.
      </p>
      <button
        onClick={handleUnsubscribe}
        disabled={status === "loading"}
        className="rounded-lg bg-red-600 px-6 py-2.5 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-60 transition-colors"
      >
        {status === "loading" ? "Unsubscribing..." : "Confirm unsubscribe"}
      </button>
      {status === "error" && (
        <p className="mt-4 text-red-600 text-sm">{errorMsg}</p>
      )}
      <a href="/" className="mt-6 text-blue-600 hover:underline text-sm">
        Keep my alerts
      </a>
    </div>
  );
}
