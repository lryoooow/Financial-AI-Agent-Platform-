import { createRoot } from "react-dom/client";
import { ThemeProvider } from "next-themes";
import App from "./app/App.tsx";
import { ErrorBoundary } from "./app/components/ErrorBoundary";
import "./styles/index.css";

createRoot(document.getElementById("root")!).render(
  <ThemeProvider attribute="class" defaultTheme="dark" enableSystem disableTransitionOnChange>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </ThemeProvider>
);
