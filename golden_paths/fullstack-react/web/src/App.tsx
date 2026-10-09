import { useEffect, useState } from "react";

import { describe, fetchHealth } from "./health";

const TITLE = "{{title}}";

export function App() {
  const [status, setStatus] = useState("Checking the backend...");

  useEffect(() => {
    fetchHealth()
      .then((health) => setStatus(describe(health)))
      .catch((error: Error) => setStatus(error.message));
  }, []);

  return (
    <main>
      <h1>{TITLE}</h1>
      <p>{status}</p>
    </main>
  );
}
