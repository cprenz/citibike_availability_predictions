import { redirect } from "next/navigation";

export default function UnsubscribePage() {
  // Standalone unsubscribe page removed — handled on the Get Alerts page.
  // Old email footer links redirect here; send them to /signup.
  redirect("/signup");
}
